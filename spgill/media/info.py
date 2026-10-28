"""
Module for reading and examining media container files.

Container of many different formats can be opened (`ffprobe` is the tool used),
but some operations can only be performed on Matroska files.
"""

# stdlib imports
import enum
import json
import packaging.version
import pathlib
import re
import typing

# vendor imports
import humanize
import langcodes
import pydantic
import rich.console
import rich.pretty
import rich.progress
import rich.prompt
import rich.text
import rich.table
import sh
from spgill.utils.walk import find_files_by_suffix
import typer
import typing_extensions

# local imports
from . import exceptions, tools

_mediainfo = sh.Command("mediainfo")
_mediainfo_min_ver = packaging.version.parse(
    exceptions.MediainfoLessThanMin._min_ver
)
_ffprobe = sh.Command("ffprobe")
_mkvextract = sh.Command("mkvextract")

_selector_fragment_pattern = re.compile(r"^([-+]?)(.*)$")
_comma_delim_nos_pattern = re.compile(
    r"^(?:(?:(?<!^),)?(?:[vas]?(?:-?\d+)?(?:(?<=\d)\:|\:(?=-?\d))(?:-?\d+)?|[vas]?-?\d+))+$"
)
_index_with_type_pattern = re.compile(r"^([vas]?)(.*?)$")

_dovi_profile_pattern = re.compile(r"dvh[e1]\.(\d{2})\.(\d{2})")
_dovi_layer_pattern = re.compile(r" (BL(?:\+EL)?(?:\+RPU)?),")


class MediaInfoTrackData(typing_extensions.TypedDict, extra_items=str):
    """Dict of key-value pairs of info from MediaInfo on a single track."""

    extra: typing.NotRequired[dict[str, str]]


class VideoCodec(enum.StrEnum):
    HEVC = "V_MPEGH/ISO/HEVC"


_subtitle_image_codecs: list[str] = ["S_HDMV/PGS", "S_VOBSUB"]
"""List of subtitle codecs that are image-based formats."""


class TrackType(enum.Enum):
    """Base types of a track."""

    General = "General"
    """Used internally for container format information"""

    Video = "Video"
    Audio = "Audio"
    Subtitle = "Text"


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

    text_descriptions: bool
    """This track contains textual descriptions of video content."""

    original_language: bool
    """This track is in the content's original language (not a translation)."""

    commentary: bool
    """This track contains commentary."""

    @classmethod
    def _parse(cls, track_info: MediaInfoTrackData) -> "TrackFlags":
        service_flags = track_info.get("ServiceKind", "").split(" / ")
        return cls(
            # Default and forced come straight from the track dict
            default=track_info.get("Default", None) == "Yes",
            forced=track_info.get("Forced", None) == "Yes",
            # The rest come from parsing the service kind field
            hearing_impaired="HI" in service_flags,
            visual_impaired="VI" in service_flags,
            text_descriptions="TD" in service_flags,
            original_language="O" in service_flags,
            commentary="C" in service_flags,
        )


class SideDataType(enum.Enum):
    """Known values of track `side_data_type`. Mostly to identify HDR and HDR-related data."""

    DolbyVisionConfig = "DOVI configuration record"
    DolbyVisionRPU = "Dolby Vision RPU Data"
    DolbyVisionMeta = "Dolby Vision Metadata"

    HDRDynamicMeta = "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"

    MasterDisplayMeta = "Mastering display metadata"
    ContentLightMeta = "Content light level metadata"

    ClosedCaptions = "ATSC A53 Part 4 Closed Captions"


class HDRFormat(enum.Enum):
    """Recognized HDR formats."""

    PQ10 = "pq10"
    HDR10 = "hdr10"
    HDR10Plus = "hdr10plus"
    DolbyVision = "dolbyvision"
    HLG = "hlg"


class DolbyVisionLayer(enum.Enum):
    BaseLayer = "BL"
    EnhancementLayer = "EL"
    RPU = "RPU"


class TrackSelectorValues(typing.TypedDict, total=True):
    """Selector flags used for simple selection of tracks from a container (specifically from the CLI)."""

    # Convenience values
    track: "Track"
    index: int
    type_order: int
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
    is_pq10: bool
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
    is_dtsx: bool
    is_truehd: bool
    is_atmos: bool
    is_object: bool

    # Subtitle track flags
    is_text: bool
    is_image: bool


def _parse_str(data: MediaInfoTrackData, key: str, fallback: str = "") -> str:
    return data.get(key, fallback)


def _parse_str_optional(
    data: MediaInfoTrackData, key: str, fallback: str = ""
) -> typing.Optional[str]:
    return data.get(key, fallback) if key in data else None


def _parse_int(data: MediaInfoTrackData, key: str, fallback: int = 0) -> int:
    return int(data.get(key, fallback))


def _parse_int_optional(
    data: MediaInfoTrackData, key: str, fallback: int = 0
) -> typing.Optional[int]:
    return int(data.get(key, fallback)) if key in data else None


def _parse_float(
    data: MediaInfoTrackData, key: str, fallback: float = 0.0
) -> float:
    return float(data.get(key, fallback))


def _parse_float_optional(
    data: MediaInfoTrackData, key: str, fallback: float = 0.0
) -> typing.Optional[float]:
    return float(data.get(key, fallback)) if key in data else None


def _parse_frame_rate_fraction(
    data: MediaInfoTrackData,
) -> typing.Optional[tuple[int, int]]:
    if "FrameRate_Num" in data and "FrameRate_Den" in data:
        return (
            _parse_int(data, "FrameRate_Num"),
            _parse_int(data, "FrameRate_Den"),
        )
    return None


def _parse_hdr_formats(track_data: MediaInfoTrackData) -> set[HDRFormat]:
    format_set: set[HDRFormat] = set()

    hdr_format_verbose = _parse_str(track_data, "HDR_Format_String")
    transfer_characteristics = _parse_str(
        track_data, "transfer_characteristics"
    )

    if "Dolby Vision" in hdr_format_verbose:
        format_set.add(HDRFormat.DolbyVision)

    # HDR10 is identified by SMPTE ST 2086
    if "SMPTE ST 2086" in hdr_format_verbose:
        format_set.add(HDRFormat.HDR10)

    # HDR10+ is identified by SMPTE ST 2094 App 4, and implies HDR10
    if "SMPTE ST 2094 App 4" in hdr_format_verbose:
        format_set.add(HDRFormat.HDR10)
        format_set.add(HDRFormat.HDR10Plus)

    # HLF is identified by the HLF transfer characteristic
    if transfer_characteristics == "HLG":
        format_set.add(HDRFormat.HLG)

    # PQ10 is PQ transfer characteristic without the HDR10 metadata
    if (
        HDRFormat.HDR10 not in format_set
        and HDRFormat.DolbyVision not in format_set
        and transfer_characteristics == "PQ"
    ):
        format_set.add(HDRFormat.PQ10)

    return format_set


class Track(pydantic.BaseModel):
    """Representation of a single track within a container. Contains all relevant attributes therein."""

    unique_id: int
    type: TrackType
    type_order: int
    index: int
    format: typing.Optional[str]
    format_commercial: typing.Optional[str]
    format_additional_features: typing.Optional[str]
    format_profile: typing.Optional[str]
    format_level: typing.Optional[str]
    format_tier: typing.Optional[str]
    codec: str
    duration: float
    bitrate: int
    size: int
    name: typing.Optional[str]
    language: typing.Optional[str]
    language_rich: typing.Any
    width: typing.Optional[int]
    height: typing.Optional[int]
    colorspace: typing.Optional[str]
    chroma_subsampling: typing.Optional[str]
    bit_depth: typing.Optional[int]
    hdr_formats: set[HDRFormat]
    channels: typing.Optional[int]
    scan_type: typing.Optional[str]
    scan_order: typing.Optional[str]
    frame_rate: typing.Optional[float]
    frame_rate_fraction: typing.Optional[tuple[int, int]]
    frame_rate_mode: typing.Optional[str]
    encoder_name: typing.Optional[str]
    encoder_settings: typing.Optional[str]

    flags: TrackFlags

    container: typing.Optional["Container"] = None
    _mediainfo_data: MediaInfoTrackData = {}

    @classmethod
    def _parse(cls, track_data: MediaInfoTrackData) -> "Track":
        track = cls(
            # common
            unique_id=_parse_int(track_data, "UniqueID"),
            type=TrackType(track_data.get("@type", None)),
            type_order=_parse_int(track_data, "@typeorder", 1) - 1,
            index=int(track_data.get("ID", 0)) - 1,
            format=_parse_str_optional(track_data, "Format"),
            format_commercial=_parse_str_optional(
                track_data, "Format_Commercial"
            )
            or _parse_str_optional(track_data, "Format_Commercial_IfAny"),
            format_additional_features=_parse_str_optional(
                track_data, "Format_AdditionalFeatures"
            ),
            format_profile=_parse_str_optional(track_data, "Format_Profile"),
            format_level=_parse_str_optional(track_data, "Format_Level"),
            format_tier=_parse_str_optional(track_data, "Format_Tier"),
            codec=track_data.get("CodecID", ""),
            duration=float(track_data.get("Duration", 0)),
            bitrate=int(track_data.get("BitRate", 0)),
            size=int(track_data.get("StreamSize", 0)),
            name=_parse_str_optional(track_data, "Title"),
            language=_parse_str(track_data, "Language", "und"),
            language_rich=langcodes.get(
                _parse_str(track_data, "Language", "und")
            ),
            flags=TrackFlags._parse(track_data),
            width=_parse_int_optional(track_data, "Width"),
            height=_parse_int_optional(track_data, "Height"),
            colorspace=_parse_str_optional(track_data, "ColorSpace"),
            chroma_subsampling=_parse_str_optional(
                track_data, "ChromaSubsampling"
            ),
            bit_depth=_parse_int_optional(track_data, "BitDepth"),
            hdr_formats=_parse_hdr_formats(track_data),
            channels=_parse_int_optional(track_data, "Channels"),
            scan_type=_parse_str_optional(track_data, "ScanType"),
            scan_order=_parse_str_optional(track_data, "ScanOrder"),
            frame_rate=_parse_float_optional(track_data, "FrameRate"),
            frame_rate_fraction=_parse_frame_rate_fraction(track_data),
            frame_rate_mode=_parse_str_optional(track_data, "FrameRate_Mode"),
            encoder_name=_parse_str_optional(
                track_data, "Encoded_Library_Name"
            ),
            encoder_settings=_parse_str_optional(
                track_data, "Encoded_Library_Settings"
            ),
        )

        track._mediainfo_data = track_data
        return track

    def __hash__(self) -> int:
        assert self.container is not None
        return self.container.format.unique_id + self.index

    def _get_extra(self) -> dict[str, str]:
        """Internal method to fetch `extra` dict from mediainfo data"""
        return self._mediainfo_data.get("extra", {})

    @property
    def is_hdr(self) -> bool:
        """
        Simple boolean property indicating if the track is encoded in an HDR format.
        """
        return bool(self.hdr_formats)

    @property
    def hdr10_metadata(
        self,
    ) -> typing.Optional[
        tuple[dict[str, typing.Any], typing.Optional[dict[str, typing.Any]]]
    ]:
        if self.type is not TrackType.Video:
            return None

        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        # Begin by searching the frame side data for the mastering display
        # metadata and the content light level metadata
        found_display_meta = list(
            self.container.get_frame_side_data(
                self, SideDataType.MasterDisplayMeta
            )
        )
        found_light_level = list(
            self.container.get_frame_side_data(
                self, SideDataType.ContentLightMeta
            )
        )

        # If the light level meta is found but not the display... Houston
        # we have a problem
        if found_light_level and not found_display_meta:
            raise exceptions.MissingMasteringDisplayMetadata(self)

        # If no display metadata is found, return nothing because this is likely
        # a PQ10 HDR video
        if not found_display_meta:
            return None

        # If not light level metadata is found, that's normal and we'll return
        # just the display metadata
        if not found_light_level:
            return (found_display_meta[0], None)

        # Ideally we have both and they can be returned
        return (found_display_meta[0], found_light_level[0])

    @property
    def hdr10_mastering_display_color_volume(self) -> typing.Optional[str]:
        """
        Return a string representing the video track's HDR10 mastering display
        color volume metadata (SMPTE ST 2086).

        Ex:

        "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"

        This string can be passed directly to libx265's `master-display` option.
        """
        if self.hdr10_metadata is None:
            return None

        display_metadata = self.hdr10_metadata[0]

        # Read all of the display master values and convert them to compatible int values
        red_x = tools.parse_display_fraction(display_metadata["red_x"], 50000)
        red_y = tools.parse_display_fraction(display_metadata["red_y"], 50000)

        green_x = tools.parse_display_fraction(
            display_metadata["green_x"], 50000
        )
        green_y = tools.parse_display_fraction(
            display_metadata["green_y"], 50000
        )

        blue_x = tools.parse_display_fraction(
            display_metadata["blue_x"], 50000
        )
        blue_y = tools.parse_display_fraction(
            display_metadata["blue_y"], 50000
        )

        white_point_x = tools.parse_display_fraction(
            display_metadata["white_point_x"], 50000
        )
        white_point_y = tools.parse_display_fraction(
            display_metadata["white_point_y"], 50000
        )

        min_luminance = tools.parse_display_fraction(
            display_metadata["min_luminance"], 10000
        )
        max_luminance = tools.parse_display_fraction(
            display_metadata["max_luminance"], 10000
        )

        # Return them all as a formatted string
        return f"G({green_x},{green_y})B({blue_x},{blue_y})R({red_x},{red_y})WP({white_point_x},{white_point_y})L({max_luminance},{min_luminance})"

    @property
    def hdr10_content_light_level(self) -> typing.Optional[str]:
        """
        Return a string representing both the video track's HDR10 maximum
        content light level metadata (MaxCLL) _and_ the maximum frame average
        light level (MaxFALL). If this metadata is not found in the video
        (somewhat uncommon) then a value of `"0,0"` will be returned.

        Example:

        “1000,400”

        This string can be passed directly to libx265's `max-cll` option.
        """
        if self.hdr10_metadata is None:
            return None

        light_level_metadata = self.hdr10_metadata[1]
        if light_level_metadata is None:
            return "0,0"

        # Parse the metadata fields and return a formatted string
        max_content_light = light_level_metadata.get("max_content", 0)
        max_average_light = light_level_metadata.get("max_average", 0)
        return f"{max_content_light},{max_average_light}"

    def _parse_dovi_profile(self) -> typing.Optional[tuple[int, int]]:
        """Internal method to parse the DoVi profile string to tuple."""
        if HDRFormat.DolbyVision in self.hdr_formats:
            hdr_format_verbose = _parse_str(
                self._mediainfo_data, "HDR_Format_String"
            )
            if match := _dovi_profile_pattern.search(hdr_format_verbose):
                return (int(match.group(1)), int(match.group(2)))
        return None

    @property
    def dovi_profile(self) -> typing.Optional[int]:
        """Return the Dolby Vision profile of the track."""
        if parsed := self._parse_dovi_profile():
            return parsed[0]
        return None

    @property
    def dovi_level(self) -> typing.Optional[int]:
        """Return the Dolby Vision level of the track."""
        if parsed := self._parse_dovi_profile():
            return parsed[1]
        return None

    @property
    def dovi_layers(self) -> set[DolbyVisionLayer]:
        """
        Return a set representing the Dolby Vision layers (BL, EL, RPU) present
        in the video stream.

        On a non-DoVi track or a non-video track, an empty set will be returned.
        """
        layers: set[DolbyVisionLayer] = set()

        if HDRFormat.DolbyVision in self.hdr_formats:
            hdr_format_verbose = _parse_str(
                self._mediainfo_data, "HDR_Format_String"
            )
            if match := _dovi_layer_pattern.search(hdr_format_verbose):
                layer_string = match.group(1)
                if "BL" in layer_string:
                    layers.add(DolbyVisionLayer.BaseLayer)
                if "EL" in layer_string:
                    layers.add(DolbyVisionLayer.EnhancementLayer)
                if "RPU" in layer_string:
                    layers.add(DolbyVisionLayer.RPU)

        return layers

    @property
    def is_atmos(self) -> bool:
        """
        Returns `True` if the track contains Dolby Atmos object-based surround
        data.

        Valid for E-AC-3 and TrueHD audio tracks.
        """
        features = (self.format_additional_features or "").split(" ")
        match self.codec:
            case "A_TRUEHD":
                return "16-ch" in features
            case "A_EAC3":
                return "JOC" in features
        return False

    @property
    def is_dts_x(self) -> bool:
        """
        Returns `True` if the track contains DTS:X object-based surround data.

        Valid only for DTS-HD MA audio tracks.
        """
        features = (self.format_additional_features or "").split(" ")
        return "X" in features

    @property
    def is_object_surround(self) -> bool:
        """
        Returns `True` if the audio track contains object-based surround data.
        """
        return self.is_atmos or self.is_dts_x

    @property
    def is_dts_hd_ma(self) -> bool:
        """
        Returns `True` if the track is a DTS HD Master Audio track.

        This is necessary because all DTS tracks from OG all the way up to
        DTS:X have the same `codec_name` value.
        """
        features = (self.format_additional_features or "").split(" ")
        return "XLL" in features

    @property
    def has_closed_captions(self) -> bool:
        """
        Returns `True` if the video track contains embedded closed captions.
        """
        if self.type is not TrackType.Video:
            return False

        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        return bool(
            list(
                self.container.get_frame_side_data(
                    self, SideDataType.ClosedCaptions
                )
            )
        )

    @property
    def is_interlaced(self) -> bool:
        """Returns `True` if the video track is interlaced."""
        return self.scan_type == "Interlaced"

    @property
    def is_progressive(self) -> bool:
        """Returns `True` if the video track is progressively scanned."""
        return not self.is_interlaced

    def __repr__(self) -> str:
        attributes = ["index", "type", "codec", "name", "language"]
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
            "type_order": self.type_order,
            "lang": self.language or "",
            "name": self.name or "",
            "codec": self.codec or "",
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
            "is_hevc": "hevc" in (self.codec or "").lower(),
            "is_avc": "avc" in (self.codec or "").lower(),
            "is_hdr": self.is_hdr,
            "is_pq10": HDRFormat.PQ10 in self.hdr_formats,
            "is_hdr10": HDRFormat.HDR10 in self.hdr_formats,
            "is_hlg": HDRFormat.HLG in self.hdr_formats,
            "is_dovi": HDRFormat.DolbyVision in self.hdr_formats,
            "is_hdr10plus": HDRFormat.HDR10Plus in self.hdr_formats,
            # Audio track flags
            "is_aac": "aac" in (self.codec or "").lower(),
            "is_ac3": "_ac3" in (self.codec or "").lower(),
            "is_eac3": "eac3" in (self.codec or "").lower(),
            "is_dts": "dts" in (self.codec or "").lower(),
            "is_dtshd": self.is_dts_hd_ma,
            "is_dtsx": self.is_dts_x,
            "is_truehd": "truehd" in (self.codec or "").lower(),
            "is_atmos": self.is_atmos,
            "is_object": self.is_object_surround,
            # Subtitle track flags
            "is_image": self.codec in _subtitle_image_codecs,
            "is_text": self.codec not in _subtitle_image_codecs,
        }

    def extract(self, path: pathlib.Path, fg: bool = True):
        """
        Extract this track to a separate file.

        *ONLY WORKS WITH MKV CONTAINERS*
        """
        if self.container is None:
            raise exceptions.TrackNoParentContainer(self)

        self.container.extract_tracks([(self, path)], fg)


# TODO: implement chapter parsing for the new backend
# class Chapter(pydantic.BaseModel):
#     """Representation of a single chapter defined within a container."""

#     id: int
#     start: int
#     start_time: str
#     end: int
#     end_time: str

#     tags: dict[str, str] = pydantic.Field(default_factory=dict)

#     @property
#     def title(self) -> typing.Optional[str]:
#         """Convenience property for reading the title tag of this chapter."""
#         return self.tags.get("title", None)


# TODO: improve chapter parsing
class Chapter(pydantic.BaseModel):
    """A single chapter in a list of chapters."""

    time: str
    language: str
    name: str


class ChapterList(pydantic.BaseModel):
    """Representation of a single chapter listing in the container."""

    chapters: list[Chapter]

    @classmethod
    def _parse(cls, data: MediaInfoTrackData):
        # Chapters are stored as entries in the extra dict
        extra = data.get("extra", {})

        chapters: list[Chapter] = []
        for time, name_full in extra.items():
            [language, name] = name_full.split(":", 1)
            chapters.append(Chapter(time=time, language=language, name=name))

        # Finally return the instantiated class
        return cls(chapters=chapters)


class ContainerFrameData(pydantic.BaseModel):
    track_index: int = pydantic.Field(alias="stream_index", default=0)

    side_data_list: list[dict[str, typing.Any]] = pydantic.Field(
        default_factory=list
    )


class Container(pydantic.BaseModel):
    """
    Object representing a single media container containing one or many individual tracks.

    Do _NOT_ instantiate this class manually, use the `Container.open()` class method instead.
    """

    _path: typing.Optional[pathlib.Path] = None

    format: Track
    chapters: list[ChapterList]

    tracks: list[Track]

    def __hash__(self) -> int:
        return self.format.unique_id

    @classmethod
    def sort_tracks_by_type(
        cls, tracks: list[Track]
    ) -> dict[TrackType, list[Track]]:
        """Return a mapping of the tracks by their assigned type."""
        return {
            track_type: [track for track in tracks if track.type is track_type]
            for track_type in [
                TrackType.Video,
                TrackType.Audio,
                TrackType.Subtitle,
            ]
        }

    @classmethod
    def sort_tracks_by_language(
        cls, tracks: list[Track]
    ) -> dict[str, list[Track]]:
        """Given a list of tracks, return them sorted by their language."""
        groups: dict[str, list[Track]] = {}

        for track in tracks:
            language = track.language or "und"
            if language not in groups:
                groups[language] = []
            groups[language].append(track)

        return groups

    @property
    def tracks_by_type(self) -> dict[TrackType, list[Track]]:
        """Container's tracks grouped by their type."""
        return self.sort_tracks_by_type(self.tracks)

    @property
    def tracks_by_language(self) -> dict[str, list[Track]]:
        """Container's tracks grouped by their language."""
        return self.sort_tracks_by_language(self.tracks)

    @property
    def tracks_by_type_by_language(
        self,
    ) -> dict[str, dict[TrackType, list[Track]]]:
        """Container's tracks grouped first by language, then by type."""
        language_groups: dict[str, dict[TrackType, list[Track]]] = {}

        for language, tracks in self.sort_tracks_by_language(
            self.tracks
        ).items():
            language_groups[language] = self.sort_tracks_by_type(tracks)

        return language_groups

    _deep_probe_track_results: typing.Optional[
        dict[Track, list[ContainerFrameData]]
    ] = None

    def _deep_probe_frames(self, track: Track):
        # Quick sanity checking
        assert self._path is not None
        assert track.container is self

        # Initialize the instance dict if not already done
        if self._deep_probe_track_results is None:
            self._deep_probe_track_results = dict()

        # If the track has not already been deep probed, then proceed
        if track not in self._deep_probe_track_results:
            try:
                # Run the ffprobe and parse it into a (partial) Container object
                results_raw = _ffprobe(
                    "-hide_banner",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-select_streams",
                    str(track.index),
                    "-show_frames",
                    "-read_intervals",
                    "%+#1",
                    "-show_entries",
                    "frame=stream_index,side_data_list",
                    "-i",
                    self._path,
                )
                assert isinstance(results_raw, str)
                results_obj = json.loads(results_raw)
                frames = pydantic.TypeAdapter(
                    list[ContainerFrameData]
                ).validate_python(results_obj.get("frames", []))
                self._deep_probe_track_results[track] = frames
            except sh.ErrorReturnCode:
                raise exceptions.ContainerCannotBeRead(self._path)

    @classmethod
    def _probe_mediainfo(cls, path: pathlib.Path) -> str:
        try:
            result = _mediainfo(
                "--Full",
                "--Output=JSON",
                path,
            )
            assert isinstance(result, str)
            return result
        except sh.ErrorReturnCode:
            raise exceptions.ContainerCannotBeRead(path)

    @classmethod
    def open(cls, path: pathlib.Path) -> "Container":
        """Open and parse a media container; returning a new `Container` instance."""

        raw_str = cls._probe_mediainfo(path)
        raw_json = json.loads(raw_str)

        # Validate that the mediainfo version is at least the minimum required
        parser_ver = packaging.version.parse(
            raw_json.get("creatingLibrary", {}).get("version", "0.0")
        )
        if parser_ver < _mediainfo_min_ver:
            raise exceptions.MediainfoLessThanMin()

        # Begin iterating through the tracks in the file
        format_info: typing.Optional[Track] = None
        chapters: list[ChapterList] = []
        track_list: list[Track] = []
        trade_data_list: list[MediaInfoTrackData] = raw_json.get(
            "media", {}
        ).get("track", [])
        for track_data in trade_data_list:
            track_type_str = track_data.get("@type", None)

            match track_type_str:
                # Intercept "General" track type which contains container format
                case "General":
                    format_info = Track._parse(track_data)

                # Set the chapters track aside
                case "Menu":
                    chapters.append(ChapterList._parse(track_data))

                # Case to handle the standard video/audio/subtitle types
                case value if value in TrackType._value2member_map_:
                    track_list.append(Track._parse(track_data))

                # Default case ignores all other types (attachments, etc)
                case _:
                    continue

        assert format_info is not None

        # Create the new container instance
        container = cls(
            format=format_info,
            chapters=chapters,
            tracks=track_list,
        )
        container._path = path

        # Bind all tracks to the container
        for track in container.tracks:
            track._bind(container)

        # Finally, return the container instance
        return container

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
        if (
            "matroska"
            not in self.format._mediainfo_data.get("Format", "").lower()
        ):
            raise exceptions.NotMatroskaContainer(self)

    def extract_tracks(
        self,
        track_pairs: list[tuple[Track, pathlib.Path]],
        /,
        warnings_are_fatal=False,
    ):
        """
        Extract one or more tracks from this container.

        *ONLY WORKS WITH MKV CONTAINERS*
        """
        assert self._path is not None
        self._assert_is_matroska()

        # Begin building a list of arguments for extraction
        extract_args: list[typing.Union[pathlib.Path, str]] = [
            str(self._path),
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
            warnings_are_fatal=warnings_are_fatal,
        )

    def extract_chapters(
        self,
        path: pathlib.Path,
        simple: bool = True,
        /,
        warnings_are_fatal=False,
    ) -> None:
        """
        Extract all chapters in this container to a file.

        *ONLY WORKS WITH MKV CONTAINERS*
        """
        self._assert_is_matroska()

        # Call mkvextract to begin the extraction
        tools.run_command_politely(
            _mkvextract,
            arguments=[
                str(self._path),
                "chapters",
                "--redirect-output",
                path,
                "--simple" if simple else "",
            ],
            cleanup_paths=[path],
            warnings_are_fatal=warnings_are_fatal,
        )

    def extract_chapters_to_string(self, simple: bool = True) -> str:
        """
        Extract all chapters in this container to a string.

        *ONLY WORKS WITH MKV CONTAINERS*
        """
        self._assert_is_matroska()

        # Call mkvextract to begin the extraction
        result = _mkvextract(
            str(self._path),
            "chapters",
            "--output-charset",
            "UTF-8",
            "--simple" if simple else "",
            _encoding="UTF-8",
        )
        assert isinstance(result, str)
        return result

    def track_belongs_to_container(self, track: Track) -> bool:
        """Return `True` if the given track exists within this container."""
        return track in self.tracks

    def get_frame_side_data(
        self, track: Track, data_type: SideDataType
    ) -> typing.Generator[dict[str, typing.Any], None, None]:
        """For a given track index, yield all side data entries that match the given type."""
        # Before perusing the frames, make sure that the track in question has
        # been deep probed for frame side data
        self._deep_probe_frames(track)

        # Identify any side data belonging to the track and yield it
        assert self._deep_probe_track_results is not None
        for frame in self._deep_probe_track_results[track]:
            if frame.track_index == track.index:
                for side_data in frame.side_data_list:
                    if side_data.get("side_data_type", "") == data_type.value:
                        yield side_data

    @property
    def title(self) -> typing.Optional[str]:
        """String title stored in the container"""
        return self.format._mediainfo_data.get("Title", None)

    @property
    def attachments(self) -> list[str]:
        """
        List of attachment names in the container.

        If none are present, an empty list will be returned.
        """
        if attachments_str := self.format._get_extra().get(
            "Attachments", None
        ):
            return attachments_str.split(" / ")
        return []


_cli_app = typer.Typer()

_default_cli_extensions: list[str] = [".mkv", ".mp4", ".m4v", ".wmv", ".avi"]

_affirmative = "[green]✓[/green]"
_negative = "[red]X[/red]"
_em_dash = "—"


@_cli_app.command(
    "tracks",
    help="Probe one or many media files and print a list of tabular readout of their"
    " various properties.",
)
def _cli_tracks(  # noqa: C901
    sources: typing.Annotated[
        list[pathlib.Path],
        typer.Argument(help="List of files and/or directories to probe."),
    ],
    recurse: typing.Annotated[
        bool,
        typer.Option(
            "--recurse",
            "-r",
            help="Recurse directory sources to look for media files. By default only files contained directly in the directory are probed.",
        ),
    ] = False,
    extensions: typing.Annotated[
        list[str],
        typer.Option(
            "--extension",
            "-x",
            help="Specify file extensions to consider when searching directories.",
        ),
    ] = _default_cli_extensions,
    selector: typing.Annotated[
        str,
        typer.Option(
            "--selector",
            "-s",
            help="Selector for deciding which tracks to show from each container. Defaults to all tracks.",
        ),
    ] = "all",
    dovi_info: typing.Annotated[
        bool,
        typer.Option(
            "--dovi",
            "-d",
            help="Display an extra column containing Dolby Vision profile, level, and layers",
        ),
    ] = False,
):
    console = rich.console.Console()

    # Construct a full list of media container paths to scan
    path_list = list(
        find_files_by_suffix(
            sources, suffixes=extensions, recurse=recurse, sort=True
        )
    )

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
    table.add_column("Features")
    table.add_column("Size", justify="right")
    table.add_column("Bitrate", justify="right")
    table.add_column("Resolution")
    table.add_column("Frame rate")
    table.add_column("HDR")

    if dovi_info:
        table.add_column("DoVi")

    table.add_column("Channels")
    table.add_column("Language")
    table.add_column("Default")
    table.add_column("Forced")
    table.add_column("HI")
    table.add_column("Commentary")
    table.add_column("Original")
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
        try:
            container = Container.open(path)
        except exceptions.ContainerCannotBeRead:
            table.add_row(f"[red]{path}[/] (READ ERROR)", end_section=True)
            continue

        # track_list = container.select_tracks(selector)
        track_list = container.tracks

        # Add a row for each track
        for j, track in enumerate(track_list):
            codec = track.codec or ""
            codec_features = []

            # Add suffix to the codec if the audio track has additional
            # information not visible in just the codec
            if track.is_atmos:
                codec_features.append("Atmos")
            if track.is_dts_hd_ma:
                codec_features.append("HD")
            if track.is_dts_x:
                codec_features.append("X")

            # If there's closed captioning embedded in the video, add a suffix
            if track.has_closed_captions:
                codec_features.append("CC")

            resolution = ""
            frame_rate = ""
            if track.width:
                resolution = f"{track.width}x{track.height}"
                if track.is_interlaced:
                    resolution += "i"
                else:
                    resolution += "p"

                frame_rate = str(track.frame_rate or "")

            hdr = ""
            dovi_col = ""
            if track.type == TrackType.Video:
                hdr = ",".join([f.name for f in (track.hdr_formats or set())])

                if dovi_info and HDRFormat.DolbyVision in track.hdr_formats:
                    layers: list[str] = []
                    if DolbyVisionLayer.BaseLayer in track.dovi_layers:
                        layers.append(DolbyVisionLayer.BaseLayer.value)
                    if DolbyVisionLayer.EnhancementLayer in track.dovi_layers:
                        layers.append(DolbyVisionLayer.EnhancementLayer.value)
                    if DolbyVisionLayer.RPU in track.dovi_layers:
                        layers.append(DolbyVisionLayer.RPU.value)

                    dovi_col = f"{track.dovi_profile}.{track.dovi_level} {'+'.join(layers)}"

            columns: list[rich.text.Text | str | None] = [
                rich.text.Text(str(path), style="yellow"),
                str(track.index),
                str(track.type.name if track.type else ""),
                str(track.type_order),
                codec,
                ",".join(codec_features) if codec_features else "",
                str(
                    humanize.naturalsize(
                        track.size,
                        binary=True,
                        gnu=True,
                    )
                    if track.size
                    else _em_dash
                ),
                str(
                    humanize.naturalsize(
                        track.bitrate,
                        binary=True,
                        gnu=True,
                    )
                    + "/s"
                    if track.bitrate
                    else _em_dash
                ),
                resolution,
                frame_rate,
                hdr,
                dovi_col if dovi_info else None,
                str(track.channels or ""),
                str(track.language or "und"),
                _affirmative if track.flags.default else _negative,
                _affirmative if track.flags.forced else _negative,
                _affirmative if track.flags.hearing_impaired else _negative,
                _affirmative if track.flags.commentary else _negative,
                _affirmative if track.flags.original_language else _negative,
                rich.text.Text(track.name or ""),
            ]

            table.add_row(
                *[c for c in columns if c is not None],
                end_section=(j == len(track_list) - 1),
            )

    console.print(table)


if __name__ == "__main__":
    _cli_app()
