"""Allow ``python3 -m shims.claude_code`` as the hook command entrypoint."""
from . import main

raise SystemExit(main())
