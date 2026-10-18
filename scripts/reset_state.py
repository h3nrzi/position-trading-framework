"""Replace an explicitly selected state file with the canonical factory snapshot."""

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile


FACTORY_PATH = Path(__file__).resolve().parents[1] / "state.factory.json"


def reset_state(destination: Path) -> None:
    """Publish the complete snapshot atomically; never truncate the destination."""
    destination = Path(destination).resolve(strict=True)
    if destination == FACTORY_PATH.resolve():
        raise ValueError("The factory snapshot cannot be a reset destination.")
    if not destination.is_file():
        raise ValueError("The reset destination must be an existing file.")

    # Read and validate before creating any output. Copy bytes instead of merging
    # fields so unknown runtime data is removed and repeated resets are identical.
    factory_bytes = FACTORY_PATH.read_bytes()
    if not isinstance(json.loads(factory_bytes), dict):
        raise ValueError("The factory snapshot must be a JSON object.")

    mode = stat.S_IMODE(destination.stat().st_mode)
    staged_path = None
    try:
        # The same directory keeps replacement on the destination's filesystem.
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.reset-",
            suffix=".tmp",
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(factory_bytes)
            staged.flush()
            os.fchmod(staged.fileno(), mode)
            os.fsync(staged.fileno())
        os.replace(staged_path, destination)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        required=True,
        type=Path,
        help="Existing state file to replace; no default destination is used.",
    )
    args = parser.parse_args()
    try:
        reset_state(args.state)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Reset failed: {error}\n")
    print(f"Factory state restored: {args.state}")


if __name__ == "__main__":
    main()
