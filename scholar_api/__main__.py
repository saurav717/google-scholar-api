import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Scholar API server")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    uvicorn.run("scholar_api.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
