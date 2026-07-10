def parse_csv(line: str) -> list[str]:
    """Split a simple comma-separated record. Quoting is intentionally out of scope."""
    return [part for part in line.split(",") if part]
