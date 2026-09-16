#!/usr/bin/env python3
"""Compatibility entry point for the offline configuration analysis."""

from generate_configuration_analysis import (  # noqa: F401
    build_report,
    load_paired_rows,
    main,
    write_analysis,
)


if __name__ == "__main__":
    raise SystemExit(main())
