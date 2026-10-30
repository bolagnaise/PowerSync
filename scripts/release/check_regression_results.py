"""Reject empty, partial or skipped suites instead of issuing passing evidence."""
import sys
import xml.etree.ElementTree as ET


def check(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {key: sum(int(s.get(key, 0)) for s in suites)
              for key in ("tests", "errors", "failures", "skipped")}
    if counts["tests"] < 8500 or any(counts[key] for key in ("errors", "failures", "skipped")):
        raise RuntimeError(f"Full regression coverage required (>=8500 tests, no skips): {counts}")
    print(f"Full regression coverage confirmed: {counts}")


if __name__ == "__main__":
    check(sys.argv[1])
