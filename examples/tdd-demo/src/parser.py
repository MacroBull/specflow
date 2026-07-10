def parse_csv(line: str) -> list[str]:
    # Intentional bug for the demo: empty fields are discarded.
    return [part for part in line.split(",") if part]
