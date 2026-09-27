import argparse
import sys


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Director CLI for Agent Director")
    parser.add_argument("--version", action="version", version="0.1.0")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Scaffold a run command
    run_parser = subparsers.add_parser("run", help="Run a workflow")
    run_parser.add_argument("workflow_name", help="Name of the workflow to run")

    parsed_args = parser.parse_args(args)

    if parsed_args.command == "run":
        print(f"Running workflow: {parsed_args.workflow_name}")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
