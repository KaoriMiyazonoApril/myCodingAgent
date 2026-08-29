"""Allow ``python -m agent`` to use the installed CLI."""

from agent.cli import main


raise SystemExit(main())
