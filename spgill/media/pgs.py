"""
This module is for parsing PGS subtitle data structures.

This code is heavily based on the phenomenal work of `pgsreader` found here:
https://raw.githubusercontent.com/EzraBC/pgsreader/refs/heads/master/pgsreader.py

I also found the following documentation of the PGS spec incredibly helpful:
https://blog.thescorpius.com/index.php/2017/07/15/presentation-graphic-stream-sup-files-bluray-subtitle-format/
"""

# stdlib imports
import dataclasses
import datetime
import enum
import io
import typing


class SegmentType(enum.IntEnum):
    """Enum mapping PGD segment types to their byte identifiers."""

    PDS = 20  # 0x14
    ODS = 21  # 0x15
    PCS = 22  # 0x16
    WDS = 23  # 0x17
    END = 128  # 0x80


class SegmentHeader(enum.Enum):
    """The individual parts of a fixed-size segment header."""

    MagicNumber = enum.auto()
    PresentationTimestamp = enum.auto()
    DecodingTimestamp = enum.auto()
    SegmentType = enum.auto()
    SegmentSize = enum.auto()


def _index_slice(n: int) -> slice:
    return slice(n, n + 1)


_segment_header_slicer_map: dict[SegmentHeader, slice] = {
    SegmentHeader.MagicNumber: slice(0, 2),
    SegmentHeader.PresentationTimestamp: slice(2, 6),
    SegmentHeader.DecodingTimestamp: slice(6, 10),
    SegmentHeader.SegmentType: _index_slice(10),
    SegmentHeader.SegmentSize: slice(11, 13),
}


class InvalidSegmentError(Exception):
    """Raised when a segment does not match PGS specification"""


class BaseSegment:
    """
    Superclass inherited by all other segment classes (PCS, WDS, PDS, ODS, END).

    Provides a set of common utility functions and properties.
    """

    _segment_header_length = 13

    @classmethod
    def _get_bytes(cls, data: bytes, slicer: slice) -> bytes:
        return data[slicer]

    @classmethod
    def _get_int(cls, data: bytes, slicer: slice) -> int:
        return int.from_bytes(cls._get_bytes(data, slicer))

    @classmethod
    def _get_header_bytes(cls, data: bytes, header: SegmentHeader) -> bytes:
        return cls._get_bytes(data, _segment_header_slicer_map[header])

    @classmethod
    def _get_header_int(cls, data: bytes, header: SegmentHeader) -> int:
        return cls._get_int(data, _segment_header_slicer_map[header])

    @classmethod
    def _make_segment(cls, chunk: bytes):
        seg_type = SegmentType(
            cls._get_int(
                chunk, _segment_header_slicer_map[SegmentHeader.SegmentType]
            )
        )
        return SegmentTypeClsMap[seg_type](chunk)

    def __init__(self, chunk: bytes):
        # Split the chunk into header and data chunks
        header = chunk[: self._segment_header_length]
        data = chunk[self._segment_header_length :]

        # Validate the magic number before continuing
        self.magic_number = self._get_header_bytes(
            header, SegmentHeader.MagicNumber
        )
        if self.magic_number != b"PG":
            raise InvalidSegmentError

        # Decode all the other fields in the segment
        # PTS and DTS are divided by 90 to account for the 90kHz accuracy
        self.presentation_timestamp_ms: float = (
            self._get_header_int(header, SegmentHeader.PresentationTimestamp)
            / 90
        )
        """Time of presentation in milliseconds"""

        self.decoding_timestamp_ms: float = (
            self._get_header_int(header, SegmentHeader.DecodingTimestamp) / 90
        )
        """Time of decoding in milliseconds (always 0 in practice)."""

        self.type: SegmentType = SegmentType(
            self._get_header_int(header, SegmentHeader.SegmentType)
        )
        self.size: int = self._get_header_int(
            header, SegmentHeader.SegmentSize
        )

        # Call the subclass post init method.
        self._post_init(data)

    def _post_init(self, data: bytes):
        """Private method to initialize segment subclasses."""

    @staticmethod
    def _format_str_timestamp(ms: float) -> str:
        stamp = str(datetime.timedelta(milliseconds=ms))

        # Ensure double zero padding
        if stamp[1] == ":":
            stamp = "0" + stamp

        # Truncate to three decimals
        return stamp[:12]

    @property
    def presentation_timestamp_str(self) -> str:
        """
        A string representation of the `presentation_timestamp` instance var.

        In the format "HH:MM:SS.sss"
        """
        return self._format_str_timestamp(self.presentation_timestamp_ms)

    def __len__(self):
        return self.size


class CompositionState(enum.IntEnum):
    Normal = 0  # 0x00
    """
    This defines a display update, and contains only functional segments with
    elements that are different from the preceding composition. It's mostly used
    to stop displaying objects on the screen by defining a composition with no
    composition objects (a value of `0` in the `num_compositions` field) but
    also used to define a new composition with new objects and objects defined
    since the Epoch Start.
    """

    AcquisitionPoint = 64  # 0x40
    """
    This defines a display refresh. This is used to compose in the middle of the
    Epoch. It includes functional segments with new objects to be used in a new
    composition, replacing old objects with the same `object_id`.
    """

    EpochStart = 128  # 0x80
    """
    This defines a new display. The Epoch Start contains all functional segments
    needed to display a new composition on the screen.
    """


@dataclasses.dataclass
class CompositionObject:
    """A single composition object found in a PCS segment."""

    object_id: int
    window_id: int
    cropped: bool
    x_offset: int
    y_offset: int

    # Fields only found on cropped compositions
    crop_x_offset: typing.Optional[int] = None
    crop_y_offset: typing.Optional[int] = None
    crop_width: typing.Optional[int] = None
    crop_height: typing.Optional[int] = None


class PresentationCompositionSegment(BaseSegment):
    """
    Class representing a segment used for composing a sub picture.
    """

    _composition_chunk_size = 8
    _composition_cropped_chunk_add_size = 8

    def _post_init(self, data: bytes):
        self.width = self._get_int(data, slice(0, 2))
        self.height = self._get_int(data, slice(2, 4))
        self.frame_rate = self._get_int(data, _index_slice(4))
        """Value is always 16"""
        self.number = self._get_int(data, slice(5, 7))
        self.state = CompositionState(self._get_int(data, _index_slice(7)))
        self.palette_update = bool(self._get_int(data, _index_slice(8)))
        self.palette_id = self._get_int(data, _index_slice(9))
        self.num_compositions = self._get_int(data, _index_slice(10))

        # The remainder of the segment data is processed as composition objects
        self.compositions: list[CompositionObject] = []
        composition_data = io.BytesIO(self._get_bytes(data, slice(11, None)))
        chunk = composition_data.read(self._composition_chunk_size)
        while chunk:
            # Build the composition object using the guaranteed data
            obj = CompositionObject(
                object_id=self._get_int(chunk, slice(0, 2)),
                window_id=self._get_int(chunk, _index_slice(2)),
                cropped=bool(self._get_int(chunk, _index_slice(3))),
                x_offset=self._get_int(chunk, slice(4, 6)),
                y_offset=self._get_int(chunk, slice(6, 8)),
            )

            # If the cropped flag is true, the composition requires an extra 8 bytes
            if obj.cropped:
                chunk += composition_data.read(
                    self._composition_cropped_chunk_add_size
                )
                obj.crop_x_offset = self._get_int(chunk, slice(8, 10))
                obj.crop_y_offset = self._get_int(chunk, slice(10, 12))
                obj.crop_width = self._get_int(chunk, slice(12, 14))
                obj.crop_height = self._get_int(chunk, slice(14, 16))

            self.compositions.append(obj)

            # Read the next chunk, which will end the loop if exhausted
            chunk = composition_data.read(self._composition_chunk_size)

        assert (
            len(self.compositions) == self.num_compositions
        ), "Number of compositions found in the segment data does not match the count provided"

    @property
    def is_start(self):
        return self.state in (
            CompositionState.EpochStart,
            CompositionState.AcquisitionPoint,
        )


@dataclasses.dataclass
class WindowObject:
    """A single window defined in a WDS segment."""

    id: int
    x_offset: int
    y_offset: int
    width: int
    height: int


class WindowDefinitionSegment(BaseSegment):
    """
    Class representing a segment used to define the rectangular area on the screen.

    This rectangular area is called a Window. This segment can define several
    windows, and all the fields from Window ID up to Window Height will repeat
    each other in the segment defining each window.
    """

    _window_chunk_size = 9

    def _post_init(self, data: bytes):
        self.num_windows = self._get_int(data, _index_slice(0))

        # The remainder of the segment data contains all of the window definitions
        self.windows: list[WindowObject] = []
        windows_data = io.BytesIO(self._get_bytes(data, slice(1, None)))
        chunk = windows_data.read(self._window_chunk_size)
        while chunk:
            self.windows.append(
                WindowObject(
                    id=self._get_int(chunk, _index_slice(0)),
                    x_offset=self._get_int(chunk, slice(1, 3)),
                    y_offset=self._get_int(chunk, slice(3, 5)),
                    width=self._get_int(chunk, slice(5, 7)),
                    height=self._get_int(chunk, slice(7, 9)),
                )
            )

            # Read the next chunk. This will end the loop when the data is exhausted
            chunk = windows_data.read(self._window_chunk_size)

        # Quick sanity check
        assert (
            len(self.windows) == self.num_windows
        ), "Number of window objects parsed does not match the number of windows expected"


# Named tuple access for static PDS palettes
@dataclasses.dataclass
class Palette:
    """A single color palette in a PDS segment"""

    ID: int

    Y: int
    Cr: int
    Cb: int
    Alpha: int


class PaletteDefinitionSegment(BaseSegment):
    """
    Class representing a segment used to define a palette for color conversion.
    """

    def _post_init(self, data: bytes):
        self.palette_id = self._get_int(data, _index_slice(0))
        self.version = self._get_int(data, _index_slice(1))

        # Parse the complete list of palette data
        chunk_size = 5
        self.palettes: list[Palette] = []
        palette_data = self._get_bytes(data, slice(2, None))
        for i in range(0, len(palette_data), chunk_size):
            chunk = palette_data[i : i + chunk_size]
            self.palettes.append(
                Palette(
                    self._get_int(chunk, _index_slice(0)),
                    self._get_int(chunk, _index_slice(1)),
                    self._get_int(chunk, _index_slice(2)),
                    self._get_int(chunk, _index_slice(3)),
                    self._get_int(chunk, _index_slice(4)),
                )
            )


class SequenceFlag(enum.IntEnum):
    Last = 64  # 0x40
    """Last fragment in sequence"""
    First = 128  # 0x80
    """First fragment in sequence"""
    FirstAndLast = 192  # 0xc0
    """First _and_ last fragment in sequence"""


class ObjectDefinitionSegment(BaseSegment):
    """
    Class representing a graphics object segment.

    These graphic objects are images with rendered text on a transparent
    background.
    """

    def _post_init(self, data: bytes):
        self.id = self._get_int(data, slice(0, 2))
        self.version = self._get_int(data, _index_slice(2))
        self.in_sequence = SequenceFlag(self._get_int(data, _index_slice(3)))

        # We subtract 4 to account for the width and height bytes
        self.image_data_len = self._get_int(data, slice(4, 7)) - 4
        self.image_width = self._get_int(data, slice(7, 9))
        self.image_height = self._get_int(data, slice(9, 11))
        self.image_data = self._get_bytes(data, slice(11, None))

        # Sanity check that the image data matches the reported length
        assert (
            len(self.image_data) == self.image_data_len
        ), "Mismatch between data length field and actual length of image data"


class EndSegment(BaseSegment):
    """End segment representing the end of a display set."""

    pass


SegmentTypeClsMap: dict[SegmentType, type[BaseSegment]] = {
    SegmentType.PDS: PaletteDefinitionSegment,
    SegmentType.ODS: ObjectDefinitionSegment,
    SegmentType.PCS: PresentationCompositionSegment,
    SegmentType.WDS: WindowDefinitionSegment,
    SegmentType.END: EndSegment,
}
"""Mapping of segment types to their respective sub-classes"""


class DisplaySet:
    """
    A class representing a single display set as parsed from a PGS stream.

    This includes at minimum a PCS and END segment, with 0 or more WDS, PDS,
    and ODS segments.
    """

    def __init__(self, segments: list[BaseSegment]) -> None:
        self.segments = segments
        self.segment_types: set[SegmentType] = set(
            [seg.type for seg in segments]
        )

        # Identify and store the assorted segment types
        self.PCS = typing.cast(
            PresentationCompositionSegment,
            next(s for s in segments if s.type is SegmentType.PCS),
        )
        self.WDS = typing.cast(
            list[WindowDefinitionSegment],
            [s for s in segments if s.type is SegmentType.WDS],
        )
        self.PDS = typing.cast(
            list[PaletteDefinitionSegment],
            [s for s in segments if s.type is SegmentType.PDS],
        )
        self.ODS = typing.cast(
            list[ObjectDefinitionSegment],
            [s for s in segments if s.type is SegmentType.ODS],
        )
        self.END = typing.cast(
            EndSegment,
            next(s for s in segments if s.type is SegmentType.END),
        )

    @property
    def timestamp_ms(self):
        """
        Timestamp in milliseconds of this display set.

        The property is derived from the lowest presentation timestamp found
        in the contained segments.
        """
        return min(seg.presentation_timestamp_ms for seg in self.segments)

    @property
    def timestamp_str(self):
        """The `timestamp_ms` represented in a string timestamp."""
        return BaseSegment._format_str_timestamp(self.timestamp_ms)

    @property
    def has_image(self):
        """True if the display set contains at least one ODS segment."""
        return SegmentType.ODS in self.segment_types

    @property
    def is_start(self):
        """Proxy for the `is_start` property of the contained PCS segment."""
        return self.PCS.is_start

    def get_palettes(self) -> list[Palette]:
        """
        Return list of all palette objects defined by any PDS segments, mapped
        against a 256 entry list by the palette's defined ID.
        """

        # Start with a list of empty palettes
        palette_list: list[Palette] = [
            Palette(i, 0, 0, 0, 0) for i in range(256)
        ]

        # Iterate through each PDS segment and insert its palettes in the correct index
        for segment in self.PDS:
            for palette in segment.palettes:
                palette_list[palette.ID] = palette

        return palette_list

class PGSReader:
    """A reader class for parsing an entire PGS data stream."""

    def __init__(self, file: typing.BinaryIO):
        self._data = file.read()

    @property
    def segments(self) -> typing.Generator[BaseSegment, None, None]:
        """
        Generator that yields sequences in-order as read from the source file.
        """

        reader = io.BytesIO(self._data)

        # Begin by reading through chunks of the data
        chunk = reader.read(BaseSegment._segment_header_length)
        while chunk:
            segment_size = BaseSegment._get_int(
                chunk, _segment_header_slicer_map[SegmentHeader.SegmentSize]
            )
            chunk += reader.read(segment_size)
            yield BaseSegment._make_segment(chunk)

            # Read the next chunk. This will end the loop when the data is exhausted.
            chunk = reader.read(BaseSegment._segment_header_length)

    @property
    def display_sets(self) -> typing.Generator[DisplaySet, None, None]:
        """
        Generator that yields display sets (logical groupings of segments) in-
        order as read from the source file.
        """

        # Iterate through segments, appending them to a list. The list is
        # yielded as a display set every time an END is encountered.
        segment_group: list[BaseSegment] = []
        for segment in self.segments:
            segment_group.append(segment)
            if segment.type is SegmentType.END:
                yield DisplaySet(segment_group)
                segment_group = []
