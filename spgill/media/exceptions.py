"""
A small collection of bespoke exceptions used internally by the `spgill.utils.media.*`
suite of modules.
"""

### stdlib imports
import typing

### vendor imports

### local imports


class _TrackException(Exception):
    # We can't use the actual track type because it would cause a circular import
    def __init__(self, track: typing.Any) -> None:
        self._track = track


class MuxDuplicateTrackFound(_TrackException):
    def __str__(self) -> str:
        return f"Tried to add track to mux job that already exists: {self._track!r}"


class MuxTrackNotFound(_TrackException):
    def __str__(self) -> str:
        return f"Could not find track in mux job: {self._track!r}"


class TrackNoParentContainer(_TrackException):
    def __str__(self) -> str:
        return f"Track has no Container bound as parent: {self._track!r}"


class MissingMasteringDisplayMetadata(_TrackException):
    def __str__(self) -> str:
        return f"Video track has content light level metadata but appears to be missing the mastering display metadata: {self._track!r}"


class _ContainerException(Exception):
    # We can't use the actual container type because it would cause a circular import
    def __init__(self, container: typing.Any) -> None:
        self._container = container


class ContainerCannotBeRead(_ContainerException):
    def __str__(self) -> str:
        return f"The file at '{self._container}' was unable to be read by 'ffprobe'. You should inspect the file for errors."


class NotMatroskaContainer(_ContainerException):
    def __str__(self) -> str:
        return f"You tried to invoke a function that can only be used on Matroska containers: {self._container!r}"


class _SelectorFragmentException(Exception):
    # We can't use the actual container type because it would cause a circular import
    def __init__(self, fragment: str) -> None:
        self._fragment = fragment


class SelectorFragmentParsingException(_SelectorFragmentException):
    def __str__(self) -> str:
        return f"Could not parse selector fragment '{self._fragment}'. Re-examine your selector syntax."


class SelectorFragmentEvalException(_SelectorFragmentException):
    def __str__(self) -> str:
        return f"Exception encountered while evaluating expression '{self._fragment}'. Re-examine your syntax."


class SelectorFragmentEvalBooleanException(_SelectorFragmentException):
    def __str__(self) -> str:
        return f"Return type of expression '{self._fragment}' was not boolean. Re-examine your syntax."
