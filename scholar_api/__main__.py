import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="scholar-api", description="Self-hosted Google Scholar API")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the HTTP server (default)")
    serve.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))

    check = sub.add_parser("check", help="test every engine against live Google Scholar")
    check.add_argument("--query", default="attention is all you need", help="search query to test with")
    check.add_argument("--author-id", default="JicYPdAAAAAJ", help="author profile to test with")
    check.add_argument("--profiles", default="geoffrey hinton", help="author search to test with")
    check.add_argument("--save-dir", default="scholar-check", help="where to save raw HTML/JSON")

    args = parser.parse_args()
    if args.command == "check":
        from .check import main as check_main

        sys.exit(check_main(args))

    import uvicorn

    host = getattr(args, "host", os.getenv("HOST", "127.0.0.1"))
    port = getattr(args, "port", int(os.getenv("PORT", "8000")))
    uvicorn.run("scholar_api.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
