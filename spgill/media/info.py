"""
Module for reading and examining media container files.

Container of many different formats can be opened (`ffprobe` is the tool used),
but some operations can only be performed on Matroska files.
"""

# stdlib imports
import enum
import pathlib
import re
import typing

# vendor imports
import humanize
import pydantic
import rich.console
import rich.progress
import rich.prompt
import rich.text
import rich.table
import sh
from spgill.utils.walk import walk
import typer

# local imports
from . import exceptions, tools

_ffprobe = sh.Command("ffprobe")
_mkvextract = sh.Command("mkvextract")

_selector_fragment_pattern = re.compile(r"^([-+]?)(.*)$")
_comma_delim_nos_pattern = re.compile(
    r"^(?:(?:(?<!^),)?(?:[vas]?(?:-?\d+)?(?:(?<=\d)\:|\:(?=-?\d))(?:-?\d+)?|[vas]?-?\d+))+$"
)
_index_with_type_pattern = re.compile(r"^([vas]?)(.*?)$")


_subtitle_image_codecs: list[str] = ["hdmv_pgs_subtitle", "dvd_subtitle"]
"""List of subtitle codecs that are image-based formats."""


class TrackType(enum.Enum):
    """Base types of a track."""

    Video = "video"
    Audio = "audio"
    Subtitle = "subtitle"
    Attachment = "attachment"


class TrackFlags(pydantic.BaseModel):
    """Boolean attribute flags of a track. Default, forced, visual impaired, etc."""

    default: bool
    """This track is eligible to be played by default."""

    forced: bool
    """This track contains onscreen text or foreign-language dialogue."""

    hearing_impaired: bool
    """This track is suitable for users with hearing impairments."""

    visual_impaired: bool
    """This track is suitable for users with visual impairments."""

    text_descriptions: bool = pydantic.Field(alias="descriptions")
    """This track contains textual descriptions of video content."""

    original_language: bool = pydantic.Field(alias="original")
    """This track is in the content's original language (not a translation)."""

    commentary: bool = pydantic.Field(alias="comment")
    """This track contains commentary."""


class SideDataType(enum.Enum):
    """Known values of track `side_data_type`. Mostly to identify HDR and HDR-related data."""

    DolbyVisionConfig = "DOVI configuration record"
    DolbyVisionRPU = "Dolby Vision RPU Data"
    DolbyVisionMeta = "Dolby Vision Metadata"

    HDRDynamicMeta = "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"

    MasterDisplayMeta = "Mastering display metadata"
    ContentLightMeta = "Content light level metadata"


class HDRFormat(enum.Enum):
    """Recognized HDR formats."""

    HDR10 = "hdr10"
    HDR10Plus = "hdr10plus"
    DolbyVision = "dolbyvision"
    HLG = "hlg"


class TrackSelectorValues(typing.TypedDict, total=True):
    """Selector flags used for simple selection of tracks from a container (specifically from the CLI)."""

    # Convenience values
    track: "Track"
    index: int
    type_index: int
    lang: str
    name: str
    codec: str

    # Convenience flags
    is_video: bool
    is_audio: bool
    is_subtitle: bool
    is_english: bool

    # Boolean disposition/flags
    is_default: bool
    is_forced: bool
    is_hi: bool
    is_commentary: bool

    # Video track flags
    is_hevc: bool
    is_avc: bool
    is_hdr: bool
    is_hdr10: bool
    is_hlg: bool
    is_dovi: bool
    is_hdr10plus: bool

    # Audio track flags
    is_aac: bool
    is_ac3: bool
    is_eac3: bool
    is_dts: bool
    is_dtshd: bool
    is_truehd: bool

    # Subtitle track flags
    is_text: bool
    is_image: bool


class TrackTags(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")

    # Important tags
    name: typing.Optional[str] = pydantic.Field(
        validation_alias=pydantic.AliasChoices("title", "TITLE"), default=None
    )
    language: typing.Optional[str] = pydantic.Field(
        validation_alias=pydantic.AliasChoices("language", "LANGUAGE"),
        default=None,
    )

    # Other meta tags. I think these only work on MKV files written with semi-recent tools.
    bps: typing.Optional[int] = pydantic.Field(alias="BPS", default=None)
    duration: typing.Optional[str] = pydantic.Field(
        alias="DURATION", default=None
    )
    number_of_frames: typing.Optional[int] = pydantic.Field(
        alias="NUMBER_OF_FRAMES", default=None
    )
    number_of_bytes: typing.Optional[int] = pydantic.Field(
        alias="NUMBER_OF_BYTES", default=None
    )

    @property
    def extras(self) -> dict[str, typing.Any]:
        """A dictionary of extra tags that are not explicitly typed in `TrackTags`"""
        return self.__pydantic_extra__ or {}


class Track(pydantic.BaseModel):
    """Representation of a single track within a container. Contains all relevant attributes therein."""

    flags: TrackFlags = pydantic.Field(alias="disposition")

    # Generic fields
    index: int
    type: TrackType = pydantic.Field(alias="codec_type")
    start_time: typing.Optional[str] = None
    codec_name: typing.Optional[str] = None
    codec_long_name: typing.Optional[str] = None
    duration: typing.Optional[str] = None
    extradata_size: typing.Optional[int] = None

    # Video fields
    width: typing.Optional[int] = None
    height: typing.Optional[int] = None
    coded_width: typing.Optional[int] = None
    coded_height: typing.Optional[int] = None
    display_aspect_ratio: typing.Optional[str] = None
    pix_fmt: typing.Optional[str] = None
    level: typing.Optional[int] = None
    field_order: typing.Optional[str] = None
    avg_frame_rate: typing.Optional[str] = None
    color_range: typing.Optional[str] = None
    color_space: typing.Optional[str] = None
    color_transfer: typing.Optional[str] = None
    color_primaries: typing.Optional[str] = None
    chroma_location: typing.Optional[str] = None

    # Audio fields
    profile: typing.Optional[str] = None
    sample_fmt: typing.Optional[str] = None
    sample_rate: typing.Optional[str] = None
    channels: typing.Optional[int] = None
    channel_layout: typing.Optional[str] = None
    bits_per_raw_sample: typing.Optional[str] = None

    tags: TrackTags = pydantic.Field(default_factory=TrackTags)
    side_data_list: list[dict[str, typing.Any]] = pydantic.Field(
        default_factory=list
    )

    container: typing.Optional["Container"] = None
    """Field linked to the parent track container object."""

    def __hash__(self) -> int:
        assert self.container
        return hash(
            f"{self.container.format.filename.absolute()}/{self.index}"
        )

    # Properties
    @property
    def language(self) -> typing.Optional[str]:
        """Convenience property for reading the language tag of this track."""
        return self.tags.language

    @property
    def name(self) -> typing.Optional[str]:
        """Convenience property for reading the name tag of this track."""
        return self.tags.name

    @property
    def type_index(self) -> int:
        """
        Property to get this track's index _in relation_ to other tracks
        of the same type.

        Requires that this `Track` instance is bound to a parent `Container`
        instance. This happens automatically if you use the `Container.open()`
        class method, but if you manually instantiate a `Track` instance you
        may have issues.
        """
        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        i: int = 0
        for track in self.container.tracks:
            if track == self:
                break
            if track.type == self.type:
                i += 1
        return i

    @property
    def hdr_formats(self) -> set[HDRFormat]:
        """
        Property containing a set of the HDR formats detected in the track.

        Only works on video tracks, and requires a bound `Container` instance.

        Warning: The first access of this property will have a slight delay as the
        container file is probed for information. This result will be cached and returned
        on further access attempts.
        """
        # HDR is (obv) only for video tracks. If this method is invoked on a non-
        # video track we will just return an empty set instead of throwing an
        # exception. This is just a cleaner operation in the end.
        if self.type is not TrackType.Video:
            return set()

        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        formats: set[HDRFormat] = set()

        # Detecting HDR10 and HLG is just a matter of reading the video track's
        # color transfer attribute
        if self.color_transfer == "smpte2084":
            formats.add(HDRFormat.HDR10)
        elif self.color_transfer == "arib-std-b67":
            formats.add(HDRFormat.HLG)

        # Dolby vision and HDR10+ can be detected via the frame side data
        if len(
            list(
                self.container.get_frame_side_data(
                    self.index, SideDataType.DolbyVisionRPU
                )
            )
        ):
            formats.add(HDRFormat.DolbyVision)

        if len(
            list(
                self.container.get_frame_side_data(
                    self.index, SideDataType.HDRDynamicMeta
                )
            )
        ):
            formats.add(HDRFormat.HDR10Plus)

        return formats

    @property
    def is_hdr(self) -> bool:
        """
        Simple boolean property indicating if the track is encoded in an HDR format.

        See `Track.hdr_formats` for warnings on access delay time.
        """
        return bool(self.hdr_formats)

    @property
    def hdr_master_display(self) -> typing.Optional[str]:
        """
        Return a string representing the video track's mastering display metadata.
        This string can be passed to libx265's `master-display` option.
        """
        if self.type is not TrackType.Video:
            return None

        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        # First we fetch the master display side data
        found_side_data = list(
            self.container.get_frame_side_data(
                self.index, SideDataType.MasterDisplayMeta
            )
        )
        if not len(found_side_data):
            return None
        display_data = found_side_data[0]

        # Read all of the display master values and convert them to compatible int values
        red_x = tools.parse_display_fraction(display_data["red_x"], 50000)
        red_y = tools.parse_display_fraction(display_data["red_y"], 50000)

        green_x = tools.parse_display_fraction(display_data["green_x"], 50000)
        green_y = tools.parse_display_fraction(display_data["green_y"], 50000)

        blue_x = tools.parse_display_fraction(display_data["blue_x"], 50000)
        blue_y = tools.parse_display_fraction(display_data["blue_y"], 50000)

        white_point_x = tools.parse_display_fraction(
            display_data["white_point_x"], 50000
        )
        white_point_y = tools.parse_display_fraction(
            display_data["white_point_y"], 50000
        )

        min_luminance = tools.parse_display_fraction(
            display_data["min_luminance"], 10000
        )
        max_luminance = tools.parse_display_fraction(
            display_data["max_luminance"], 10000
        )

        # Return them all as a formatted string
        return f"G({green_x},{green_y})B({blue_x},{blue_y})R({red_x},{red_y})WP({white_point_x},{white_point_y})L({max_luminance},{min_luminance})"

    @property
    def hdr_content_light_level(self) -> typing.Optional[str]:
        """
        Return a string representing the video track's content light level data.
        This string can be passed to libx265's `max-cll` option.
        """
        if self.type is not TrackType.Video:
            return None

        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        # First we fetch the master display side data
        found_side_data = list(
            self.container.get_frame_side_data(
                self.index, SideDataType.ContentLightMeta
            )
        )
        if not len(found_side_data):
            return None
        light_level_data = found_side_data[0]

        # Parse the metadata fields and return a formatted string
        max_content_light = light_level_data.get("max_content", 0)
        max_average_light = light_level_data.get("max_average", 0)
        return f"{max_content_light},{max_average_light}"

    def __repr__(self) -> str:
        attributes = ["index", "type", "codec_name", "name", "language"]
        formatted_attributes: list[str] = []
        for name in attributes:
            formatted_attributes.append(f"{name}={getattr(self, name)!r}")
        return f"{type(self).__name__}({', '.join(formatted_attributes)})"

    def _bind(self, container: "Container") -> None:
        self.container = container

    def get_selector_values(self) -> TrackSelectorValues:
        """
        Return a dictionary mapping of computed track selector values.
        """
        return {
            # Convenience values
            "track": self,
            "index": self.index,
            "type_index": self.type_index,
            "lang": self.language or "",
            "name": self.name or "",
            "codec": self.codec_name or "",
            # Convenience flags
            "is_video": self.type is TrackType.Video,
            "is_audio": self.type is TrackType.Audio,
            "is_subtitle": self.type is TrackType.Subtitle,
            "is_english": (self.language or "").lower() in ["en", "eng"],
            # Boolean disposition flags
            "is_default": self.flags.default,
            "is_forced": self.flags.forced,
            "is_hi": self.flags.hearing_impaired,
            "is_commentary": self.flags.commentary,
            # Video track flags
            "is_hevc": "hevc" in (self.codec_name or "").lower(),
            "is_avc": "avc" in (self.codec_name or "").lower(),
            "is_hdr": self.is_hdr,
            "is_hdr10": HDRFormat.HDR10 in self.hdr_formats,
            "is_hlg": HDRFormat.HLG in self.hdr_formats,
            "is_dovi": HDRFormat.DolbyVision in self.hdr_formats,
            "is_hdr10plus": HDRFormat.HDR10Plus in self.hdr_formats,
            # Audio track flags
            "is_aac": "aac" in (self.codec_name or "").lower(),
            "is_ac3": "_ac3" in (self.codec_name or "").lower(),
            "is_eac3": "eac3" in (self.codec_name or "").lower(),
            "is_dts": "dts" in (self.codec_name or "").lower(),
            "is_dtshd": "dts" in (self.codec_name or "").lower()
            and "hd" in (self.profile or "").lower(),
            "is_truehd": "truehd" in (self.codec_name or "").lower(),
            # Subtitle track flags
            "is_image": self.codec_name in _subtitle_image_codecs,
            "is_text": self.codec_name not in _subtitle_image_codecs,
        }

    def extract(self, path: pathlib.Path, fg: bool = True):
        """
        Extract this track to a new file.

        *ONLY WORKS WITH MKV CONTAINERS*
        """
        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        self.container.extract_tracks([(self, path)], fg)


class Chapter(pydantic.BaseModel):
    """Representation of a single chapter defined within a container."""

    id: int
    start: int
    start_time: str
    end: int
    end_time: str

    tags: dict[str, str] = pydantic.Field(default_factory=dict)

    @property
    def title(self) -> typing.Optional[str]:
        """Convenience property for reading the title tag of this chapter."""
        return self.tags.get("title", None)


class ContainerFormat(pydantic.BaseModel):
    """Format metadata of a container"""

    filename: pathlib.Path  # Cast from str
    tracks_count: int = pydantic.Field(alias="nb_streams")
    # programs_count: int = pydantic.Field(alias="nb_programs")  # Still not sure what "programs" are
    format_name: str
    format_long_name: str
    start_time: typing.Optional[str] = None
    duration: typing.Optional[str] = None
    size: int  # Cast from str
    bit_rate: typing.Optional[int] = None  # Cast from str
    probe_score: int

    tags: dict[str, str] = pydantic.Field(default_factory=dict)


class ContainerFrameData(pydantic.BaseModel):
    track_index: int = pydantic.Field(alias="stream_index", default=0)

    side_data_list: list[dict[str, typing.Any]] = pydantic.Field(
        default_factory=list
    )


class Container(pydantic.BaseModel):
    """Do NOT instantiate this class manually, use the `Container.open()` class method instead."""

    format: ContainerFormat
    tracks: list[Track] = pydantic.Field(
        alias="streams"
    )  # We alias this to "streams", because we prefer mkv terminology
    chapters: list[Chapter]

    frames: list[ContainerFrameData]
    """List of frame data captured from `ffprobe`. Only useful for identifying frame side data."""

    def __hash__(self) -> int:
        return hash(self.format.filename.absolute())

    @property
    def tracks_by_type(self) -> dict[TrackType, list[Track]]:
        """Property with a dictionary that groups tracks by their type."""
        groups: dict[TrackType, list[Track]] = {
            TrackType.Video: [],
            TrackType.Audio: [],
            TrackType.Subtitle: [],
            TrackType.Attachment: [],
        }

        for track in self.tracks:
            groups[track.type].append(track)

        return groups

    @classmethod
    def _probe(cls, path: pathlib.Path) -> str:
        probe_result = _ffprobe(
            "-hide_banner",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-show_frames",
            "-read_intervals",
            "%+#10",
            "-show_entries",
            "frame=stream_index,side_data_list",
            "-i",
            path,
        )
        assert isinstance(probe_result, str)
        return probe_result

    @classmethod
    def open(cls, path: pathlib.Path) -> "Container":
        """Open a media container by its path and return a new `Container` instance."""
        raw_json = cls._probe(path)

        # Parse the JSON into a new instance
        instance = Container.model_validate_json(raw_json)
        assert not isinstance(instance, list)

        # Bind all the tracks back to this container
        for track in instance.tracks:
            track._bind(instance)

        return instance

    @staticmethod
    def select_tracks_from_list(  # noqa: C901
        track_list: list[Track], selector: typing.Optional[str]
    ) -> list[Track]:
        """
        Given a list of `MediaTrack`'s, return a selection of these tracks defined
        by a `selector` following a particular syntax.

        ### The selector must obey one of the following rules:

        The selection starts with _no_ tracks selected.

        A constant value:
        - `"none"` or empty string or `None`, returns nothing (an empty array).
        - `"all"` will return all the input tracks (cloning the array).

        A comma-delimited list of indexes and/or slices:
        - These indexes are in reference to the list of tracks passed to the method.
        - No spaces allowed!
        - Slices follow the same rules and basic syntax as Python slices.
          E.g. `1:3` or `:-1`
        - If the index/slice begins with one of `v` (video), `a` (audio), or
          `s` (subtitle) then the index/range will be taken from only tracks
          of that type (wrt the order they appear in the list).

        A colon-delimitted list of python expressions:
        - Each expression either adds to the selection or removes from it.
          - This is defined by starting your expression with an operator; `+` or `-`.
          - `+` is implied if no operator is given.
        - Each expression must return a boolean value.
        - `"all"` is a valid expression and will add or remove (why?) all tracks from the selection.
        - There are lots of pre-calculated boolean flags and other variables available
          during evaluation of your expression. Inspect source code of this method
          to learn all of the available variables.
        - Examples;
          - `+isEnglish`, include only english language tracks.
          - `+all:-isPGS` or `+!isPGS`, include only non-PGS subtitle tracks.
          - `+isTrueHD:+'commentary' in title.lower()`. include Dolby TrueHD tracks and any tracks labelled commentary.
        """
        # "none" is a valid selector. Returns an empty list.
        # Empty or falsy strings are treated the same as "none"
        if selector == "none" or not selector:
            return []

        # ... As is "all". Returns every track passed in.
        if selector == "all":
            return track_list.copy()

        # The selector may also be a comma delimited list of track indexes and ranges.
        if _comma_delim_nos_pattern.match(selector):
            # Create a quick mapping of track types to tracks
            grouped_tracks: dict[TrackType, list[Track]] = {
                TrackType.Video: [],
                TrackType.Audio: [],
                TrackType.Subtitle: [],
            }
            for track in track_list:
                if (
                    group_list := grouped_tracks.get(track.type, None)
                ) is not None:
                    group_list.append(track)

            indexed_tracks: list[Track] = []

            # Iterate through the arguments in the list
            for fragment in selector.split(","):
                fragment_match = _index_with_type_pattern.match(fragment)
                assert fragment_match is not None
                argument_type, argument = fragment_match.groups()

                # If a type is specified, we need to change where the tracks
                # are selected from
                track_source = track_list
                if argument_type == "v":
                    track_source = grouped_tracks[TrackType.Video]
                elif argument_type == "a":
                    track_source = grouped_tracks[TrackType.Audio]
                elif argument_type == "s":
                    track_source = grouped_tracks[TrackType.Subtitle]

                # If there is a colon character, the argument is a range
                if ":" in argument:
                    start, end = (
                        (int(s) if s else None) for s in argument.split(":")
                    )
                    for track in track_source[start:end]:
                        indexed_tracks.append(track)

                # Else, it's just a index number
                else:
                    indexed_tracks.append(track_source[int(argument)])

            # Return it as an iteration of the master track list so that it
            # maintains the original order
            return [track for track in track_list if track in indexed_tracks]

        # Start with an empty list
        selected_tracks: list[Track] = []

        # Split the selector string into a list of selector fragments
        selector_fragments = selector.split(":")

        # Iterate through each fragment consecutively and evaluate them
        for fragment in selector_fragments:
            try:
                fragment_match = _selector_fragment_pattern.match(fragment)

                if fragment_match is None:
                    raise exceptions.SelectorFragmentParsingException(fragment)

                polarity, expression = fragment_match.groups()
            except AttributeError:
                raise exceptions.SelectorFragmentParsingException(fragment)

            filtered_tracks: list[Track] = []

            if expression == "all":
                filtered_tracks = track_list

            # Iterate through each track and apply the specified expression to filter
            else:
                for track in track_list:
                    # Evaluate the expression
                    try:
                        eval_result = eval(
                            expression, None, track.get_selector_values()
                        )
                    except Exception:
                        raise exceptions.SelectorFragmentEvalException(
                            expression
                        )

                    # If the result isn't a boolean, raise an exception
                    if not isinstance(eval_result, bool):
                        raise exceptions.SelectorFragmentEvalBooleanException(
                            expression
                        )

                    if eval_result:
                        filtered_tracks.append(track)

            # If polarity is positive, add the filtered tracks into the selected tracks
            # list, in its original order.
            if not polarity or polarity == "+":
                selected_tracks = [
                    track
                    for track in track_list
                    if (track in filtered_tracks or track in selected_tracks)
                ]

            # Else, filter the selected tracks list by the filtered tracks
            else:
                selected_tracks = [
                    track
                    for track in selected_tracks
                    if track not in filtered_tracks
                ]

        return selected_tracks

    def select_tracks(self, selector: str) -> list[Track]:
        """
        Select tracks from this container using a selector string.

        More information on the syntax of the selector string can be found
        in the docstring of the `Container.select_tracks_from_list` method.
        """
        return self.select_tracks_from_list(self.tracks, selector)

    def select_tracks_by_type(
        self, type: TrackType, selector: str
    ) -> list[Track]:
        """
        Select tracks--of only a particular type--from this container using a selector string.

        More information on the syntax of the selector string can be found
        in the docstring of the `Container.select_tracks_from_list` method.
        """
        return self.select_tracks_from_list(
            self.tracks_by_type[type], selector
        )

    def _assert_is_matroska(self):
        if "matroska" not in self.format.format_name.lower():
            raise exceptions.NotMatroskaContainer(self)

    def extract_tracks(
        self, track_pairs: list[tuple[Track, pathlib.Path]], fg: bool = True
    ):
        """
        Extract one or more tracks from this container.

        *ONLY WORKS WITH MKV CONTAINERS*
        """
        self._assert_is_matroska()

        # Begin building a list of arguments for extraction
        extract_args: list[typing.Union[pathlib.Path, str]] = [
            self.format.filename,
            "tracks",
        ]

        # Iterate through each tuple given and generator appropriate arguments
        for track, path in track_pairs:
            # Assert the track belongs to this container
            assert track.container is self

            extract_args.append(f"{track.index}:{path}")

        # Execute the extraction commands
        tools.run_command_politely(
            _mkvextract,
            arguments=extract_args,
            cleanup_paths=[pair[1] for pair in track_pairs],
        )

    def extract_chapters(
        self, path: pathlib.Path, simple: bool = False, fg: bool = True
    ):
        """
        Extract all chapters in this container to a file.

        *ONLY WORTH WITH MKV CONTAINERS*
        """
        self._assert_is_matroska()

        # Call mkvextract to begin the extraction
        tools.run_command_politely(
            _mkvextract,
            arguments=[path, "chapters", "--simple" if simple else ""],
            cleanup_paths=[path],
        )

    def track_belongs_to_container(self, track: Track) -> bool:
        """Return `True` if the given track exists within this container."""
        return track in self.tracks

    def get_frame_side_data(
        self, track_index: int, data_type: SideDataType
    ) -> typing.Generator[dict[str, typing.Any], None, None]:
        """For a given track index, yield all side data entries that match the given type."""
        for frame in self.frames:
            if frame.track_index == track_index:
                for side_data in frame.side_data_list:
                    if side_data.get("side_data_type", "") == data_type.value:
                        yield side_data


_default_cli_extensions: list[str] = [".mkv", ".mp4", ".m4v", ".wmv", ".avi"]

_affirmative = "[green]✓[/green]"
_negative = "[red]✗[/red]"
_em_dash = "—"


def _main(
    sources: list[pathlib.Path] = typer.Argument(
        ..., help="List of files and/or directories to probe."
    ),
    recurse: bool = typer.Option(
        False,
        "--recurse",
        "-r",
        help="Recurse directory sources to look for media files. By default only files contained directly in the directory are probed.",
    ),
    extensions: list[str] = typer.Option(
        _default_cli_extensions,
        "--extension",
        "-x",
        help="Specify file extensions to consider when searching directories.",
    ),
    selector: str = typer.Option(
        "all",
        "--selector",
        "-s",
        help="Selector for deciding which tracks to show from each container. Defaults to all tracks.",
    ),
):
    """
    CLI interface for probing media containers and printing info about the container
    and their tracks.
    """

    console = rich.console.Console()

    # Construct a full list of media container paths to scan
    path_list: list[pathlib.Path] = []
    for source in sources:
        if source.is_file():
            path_list.append(source)
        elif recurse:
            for *_, paths in walk(source, sort=True):
                for path in paths:
                    if path.suffix.lower() in extensions:
                        path_list.append(path)
        else:
            for path in sorted(source.iterdir()):
                if path.suffix.lower() == ".mkv":
                    path_list.append(path)

    # If no file were found in the sweep, abort
    if not len(path_list):
        console.print("[red italic]No files found. Aborting!")
        exit()

    # Construct the table and its header
    table = rich.table.Table()
    table.add_column("File")
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("Order")
    table.add_column("Codec")
    table.add_column("Mode")
    table.add_column("Bitrate", justify="right")
    table.add_column("Resolution")
    table.add_column("HDR")
    table.add_column("Channels")
    table.add_column("Language")
    table.add_column("Default")
    table.add_column("Forced")
    table.add_column("HI")
    table.add_column("Commentary")
    table.add_column("Title")

    # Iterate through each media file and list the audio tracks
    for i, path in enumerate(
        rich.progress.track(
            path_list,
            console=console,
            description="Gathering media information...",
            transient=True,
        )
    ):
        container = Container.open(path)
        track_list = container.select_tracks(selector)

        # Add a row for each track
        for j, track in enumerate(track_list):
            resolution = ""
            if track.width:
                resolution = f"{track.width}x{track.height}"
                if track.field_order in ["tt", "bb", "tb", "bt"]:
                    resolution += "i"
                else:
                    resolution += "p"

                if track.avg_frame_rate:
                    try:
                        resolution += str(round(eval(track.avg_frame_rate), 2))
                    except ZeroDivisionError:
                        pass

            hdr = ""
            if track.type == TrackType.Video:
                hdr = ",".join([f.name for f in (track.hdr_formats or set())])

            table.add_row(
                str(path),
                str(track.index),
                str(track.type.name if track.type else ""),
                str(track.type_index),
                str(track.codec_name),
                str(track.sample_fmt or _em_dash),
                str(
                    humanize.naturalsize(
                        int(track.bits_per_raw_sample or 0),
                        binary=True,
                        gnu=True,
                    )
                    + "/s"
                    if track.bits_per_raw_sample
                    else _em_dash
                ),
                resolution,
                hdr,
                str(track.channels or ""),
                str(track.language or "und"),
                _affirmative if track.flags.default else _negative,
                _affirmative if track.flags.forced else _negative,
                _affirmative if track.flags.hearing_impaired else _negative,
                _affirmative if track.flags.commentary else _negative,
                track.name or "",
                end_section=(j == len(track_list) - 1),
            )

    console.print(table)


if __name__ == "__main__":
    typer.run(_main)
