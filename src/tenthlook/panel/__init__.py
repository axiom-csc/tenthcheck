"""Panel pressure battery: does a chat model keep a correct answer under scripted pushback?"""
__version__ = "0.1.0"


def guard_data_dir():
    """The support guard's sample/held-out data: <repo>/panel/guard_data in a source checkout, else the two sample files shipped in the package."""
    from pathlib import Path
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "panel" / "guard_data").is_dir() and (p / "src" / "tenthlook").is_dir():
            return p / "panel" / "guard_data"
    return here.parent / "guard" / "data"
