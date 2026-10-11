import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "boptest_simulator.app:app",
        host=os.environ.get("BOPTEST_SIMULATOR_HOST", "127.0.0.1"),
        port=int(os.environ.get("BOPTEST_SIMULATOR_PORT", "8001")),
        workers=1,
    )


if __name__ == "__main__":
    main()
