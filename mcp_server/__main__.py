"""Allow ``python3 -m mcp_server`` launch (the stdio sidecar invocation)."""
from . import main

raise SystemExit(main())
