"""Indradhanu OS backend.

Python 3.11 or newer is required, and the check below is here so that running
on an older interpreter fails with a sentence that explains itself rather than
with `ImportError: cannot import name 'UTC' from 'datetime'` forty frames deep.

The two features that set the floor are `datetime.UTC` and `enum.StrEnum`, both
added in 3.11 and both used throughout the hazard adapters, the wire models and
the auth layer.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise RuntimeError(
        "Indradhanu OS needs Python 3.11 or newer; this interpreter is "
        f"{sys.version_info.major}.{sys.version_info.minor}."
        "\n"
        "\nThe codebase uses datetime.UTC and enum.StrEnum, which do not exist"
        "\nbefore 3.11. Recreate the virtual environment on a newer interpreter:"
        "\n"
        "\n    py -3.12 -m venv .venv"
        "\n    .venv\\Scripts\\Activate.ps1"
        "\n    pip install -r requirements-dev.txt"
        "\n"
    )
