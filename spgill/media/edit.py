"""
This module provides an interface for editing headers of a Matroska (aka MKV)
container file.

Currently allows for editing tags, chapters, boolean flags, and named string
parameters of tracks.
"""

### stdlib imports
import enum
import pathlib
import tempfile
import typing

### vendor imports
import sh

### local imports
from spgill.media import info, tools

# Commands
_mkvpropedit = sh.Command("mkvpropedit")


class TagSelector(enum.Enum):
    """Selector for tags in a container that aren't bound to a particular track."""

    All = "all"
    """Selector for the tags of all tracks in the container."""

    Global = "global"
    """Selector for the global container tags."""


class TrackFlag(enum.Enum):
    """Boolean track flag attributes."""

    Default = "flag-default"
    """This track is eligible to be played by default."""

    Enabled = "flag-enabled"
    """Legacy option for compatibility. Best not to be used."""

    Forced = "flag-forced"
    """This track contains onscreen text or foreign-language dialogue."""

    HearingImpaired = "flag-hearing-impaired"
    """This track is suitable for users with hearing impairments."""

    VisualImpaired = "flag-visual-impaired"
    """This track is suitable for users with visual impairments."""

    TextDescriptions = "flag-text-descriptions"
    """This track contains textual descriptions of video content."""

    OriginalLanguage = "flag-original"
    """This track is in the content's original language (not a translation)."""

    Commentary = "flag-commentary"
    """This track contains commentary."""


class TrackProperty(enum.Enum):
    """Common track properties to be edited."""

    Name = "name"
    Language = "language"
    LanguageIETF = "language-ietf"


class _PropertyAction(enum.Enum):
    """Action taken on a track property."""

    Add = enum.auto()
    Set = enum.auto()
    Delete = enum.auto()


def _track_selector(
    track: info.Track,
) -> str:
    """Return selector string for a track."""
    return f"track:{track.index + 1}"


def _generate_property_arguments(
    action: _PropertyAction,
    property_name: str,
    property_value: typing.Optional[typing.Union[bool, str, int]],
) -> typing.Generator[str, None, None]:
    """Generate arguments for an action on a track property."""
    formatted_value: str = str(property_value)
    if isinstance(property_value, bool):
        formatted_value = str(int(property_value))

    if action is _PropertyAction.Add:
        yield from ["--add", f"{property_name}={formatted_value}"]
    elif action is _PropertyAction.Set:
        yield from ["--set", f"{property_name}={formatted_value}"]
    elif action is _PropertyAction.Delete:
        yield from ["--delete", property_name]


class EditJob:
    """Class for defining and executing a job to edit the headers of an Matroska container file."""

    # List of stream types that can be edited (excludes attachment types)
    _accepted_stream_types: list[info.TrackType] = [
        info.TrackType.Video,
        info.TrackType.Audio,
        info.TrackType.Subtitle,
    ]

    # Instance vars storing all changes that will be made
    _tag_actions: list[
        tuple[
            _PropertyAction,
            typing.Union[TagSelector, info.Track],
            typing.Optional[pathlib.Path],
        ]
    ]
    _chapter_action: typing.Optional[
        tuple[_PropertyAction, typing.Optional[pathlib.Path]]
    ]
    _chapter_tempfile: typing.Optional[tempfile._TemporaryFileWrapper]
    _container_title_action: typing.Optional[
        tuple[_PropertyAction, typing.Union[str, None]]
    ]
    _track_actions: dict[
        info.Track,
        list[
            tuple[
                str,
                _PropertyAction,
                typing.Optional[typing.Union[bool, str, int]],
            ],
        ],
    ]

    def __init__(self, container: info.Container) -> None:
        self._container = container

        # Sanity check that this is an MKV container. No other formats may be edited.
        assert "matroska" in container.format.format_name.lower()

        # Initialize instance vars
        self._tag_actions = []
        self._chapter_action = None
        self._chapter_tempfile = None
        self._container_title_action = None
        self._track_actions = {}

    def set_tags(
        self,
        selector: typing.Union[TagSelector, info.Track],
        path: pathlib.Path,
    ):
        """Set tags; globally, for all tracks, or just a single track."""
        self._tag_actions.append((_PropertyAction.Set, selector, path))

    def delete_tags(
        self,
        selector: typing.Union[TagSelector, info.Track],
    ):
        """Delete tags; globally, for all tracks, or just a single track."""
        self._tag_actions.append((_PropertyAction.Delete, selector, None))

    def set_chapters(self, path: pathlib.Path) -> None:
        """Set the container's chapters from a file. Mutually exclusive with `EditJob.remove_chapters()`."""
        self._chapter_action = (_PropertyAction.Set, path)

    def set_chapters_from_string(self, chapters: str) -> None:
        """Set the container's chapters from a string instead of a file."""
        self._chapter_tempfile = tempfile.NamedTemporaryFile(mode="wb")
        self._chapter_tempfile.write(chapters.encode("UTF-8"))
        self._chapter_tempfile.flush()
        self.set_chapters(pathlib.Path(self._chapter_tempfile.name))

    def delete_chapters(self):
        """Remove all chapters from the container. Mutually exclusive with `EditJob.set_chapters()`."""
        self._chapter_action = (_PropertyAction.Delete, None)

    def set_container_title(self, title: str) -> None:
        """Set a new title for the container."""
        self._container_title_action = (_PropertyAction.Set, title)

    def delete_container_title(self) -> None:
        """Delete the container's title property."""
        self._container_title_action = (_PropertyAction.Delete, None)

    def _modify_track_flag(
        self,
        track: info.Track,
        flag: TrackFlag,
        action: _PropertyAction,
        value: typing.Optional[bool],
    ) -> None:
        if track not in self._track_actions:
            self._track_actions[track] = []
        self._track_actions[track].append((flag.value, action, value))

    def set_track_flag(
        self, track: info.Track, flag: TrackFlag, value: bool
    ) -> None:
        """Set a flag value on a track."""
        self._modify_track_flag(track, flag, _PropertyAction.Set, value)

    def delete_track_flag(self, track: info.Track, flag: TrackFlag) -> None:
        """Delete a flag value from a track."""
        self._modify_track_flag(track, flag, _PropertyAction.Delete, None)

    def _modify_track_property(
        self,
        track: info.Track,
        property: typing.Union[TrackProperty, str],
        action: _PropertyAction,
        value: typing.Optional[typing.Union[str, int]],
    ) -> None:
        if track not in self._track_actions:
            self._track_actions[track] = []
        property_name: str = (
            property.value if isinstance(property, TrackProperty) else property
        )
        self._track_actions[track].append((property_name, action, value))

    def set_track_property(
        self,
        track: info.Track,
        property: typing.Union[TrackProperty, str],
        value: typing.Union[str, int],
    ) -> None:
        """Set a string/integer property on a track."""
        self._modify_track_property(
            track, property, _PropertyAction.Set, value
        )

    def add_track_property(
        self,
        track: info.Track,
        property: typing.Union[TrackProperty, str],
        value: typing.Union[str, int],
    ) -> None:
        """
        Add a string/integer property to a track. Only certain properties support multiple values.

        NOTE: This should not be conflated with the `EditJob.set_track_property()` function. This
        function should only be used for adding consecutive values to a property of the same name.
        """
        self._modify_track_property(
            track, property, _PropertyAction.Add, value
        )

    def delete_track_property(
        self,
        track: info.Track,
        property: typing.Union[TrackProperty, str],
    ) -> None:
        """Delete a property from a track."""
        self._modify_track_property(
            track, property, _PropertyAction.Delete, None
        )

    def _generate_tag_arguments(
        self,
    ) -> typing.Generator[str, None, None]:
        for action, selector, path in self._tag_actions:
            selector_name: str = (
                _track_selector(selector)
                if isinstance(selector, info.Track)
                else selector.value
            )

            if action is _PropertyAction.Set:
                yield from ["--tags", f"{selector_name}:{path}"]

            elif action is _PropertyAction.Delete:
                yield from ["--tags", f"{selector_name}:"]

    def _generate_chapter_arguments(
        self,
    ) -> typing.Generator[str, None, None]:
        if self._chapter_action:
            action, path = self._chapter_action

            yield "--chapters"

            if action is _PropertyAction.Set:
                yield str(path)

            elif action is _PropertyAction.Delete:
                yield ""

    def _generate_container_arguments(
        self,
    ) -> typing.Generator[str, None, None]:
        if self._container_title_action:
            yield from ["--edit", "info"]
            yield from _generate_property_arguments(
                self._container_title_action[0],
                "title",
                self._container_title_action[1],
            )

    def _generate_track_arguments(self) -> typing.Generator[str, None, None]:
        for track, actions_list in self._track_actions.items():
            yield from ["--edit", _track_selector(track)]
            for name, action, value in actions_list:
                yield from _generate_property_arguments(action, name, value)

    def _generate_command_arguments(self) -> typing.Generator[str, None, None]:
        # Start with the filename
        yield str(self._container.format.filename)

        yield from self._generate_tag_arguments()
        yield from self._generate_chapter_arguments()
        yield from self._generate_container_arguments()
        yield from self._generate_track_arguments()

    def run(self):
        """Execute the edit operation. Blocks until the command process exits."""
        tools.run_command_politely(
            _mkvpropedit,
            arguments=list(self._generate_command_arguments()),
        )
