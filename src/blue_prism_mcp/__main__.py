"""``python -m blue_prism_mcp`` — the module entrypoint.

Delegates to the same main() the ``blue-prism-mcp`` console script runs, so the
server can be launched without the script being on PATH (cleaner for embedding,
containers, and the end-to-end tests, which spawn the module by interpreter).
"""

from .server import main

if __name__ == "__main__":
    main()
