"""
Module containing miscellaneous utility functions that don't belong anywhere else.
"""

### stdlib imports
import pathlib
import re

### vendor imports
import charset_normalizer


def guess_subtitle_charset(
    path: pathlib.Path,
    /,
    confidence_threshold: float = 0.5,
    ignore_low_confidence: bool = False,
    default_charset: str = "UTF-8",
) -> str:
    """
    Guess the charset of a TEXT subtitle file.

    Useful when muxing .SRT or other text subtitles into a Matroska container,
    because Matroska will assume everything is UTF-8; this tool can help identify
    if a character set needs to be converted in the mux process.

    Args:
        path: Path of the subtitle fine to analyze.
        confidence_threshold (optional): Lower threshold of confidence when guessing
            the character set. If the confidence is less than or equal to this value
            then either (1) an exception will be raised or (2) the `default_charset`
            argument will be returned; which behavior will be dependent on other
            arguments.
        ignore_low_confidence (optional): If `True`, a low confidence scenario will
            not result in an exception and will instead return the `default_charset`
            argument's value.
        default_charset (optional): This is the default character set that
            will be returned if the character set cannot be confidently guessed.
    """
    with path.open("rb") as handle:
        results = charset_normalizer.detect(handle.read())

    confidence, encoding = results["confidence"], results["encoding"]
    assert isinstance(confidence, float) and isinstance(encoding, str)

    if confidence <= confidence_threshold:
        if ignore_low_confidence:
            return default_charset
        raise RuntimeError(
            f"Lack of confidence detecting charset for '{path}'. "
            "There may not be enough text to make an accurate assessment. "
            "You can invoke the method with `ignore_low_confidence=True` to suppress this warning "
            "or you can tune the confidence threshold with the `confidence_threshold` argument."
        )

    return encoding


_display_color_pattern = re.compile(r"^(\d+)\/(\d+)$")


def parse_display_fraction(value: str, ideal_denominator: int) -> int:
    """
    Utility function to parse a master display fraction (e.g. "13250/50000") and
    match the numerator to an ideal denominator value.
    """
    if match := _display_color_pattern.match(value):
        numerator, denominator = (int(n) for n in match.groups())

        if denominator != ideal_denominator:
            return int((ideal_denominator / denominator) * numerator)
        return numerator
    return 0
