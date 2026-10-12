import pytest

# Async tests run on anyio's pytest plugin (installed with mcp) instead of pytest-asyncio, because
# anyio keeps a fixture's setup and teardown in the same task as the test, which the MCP client's
# cancel scopes require. Each test module opts in with `pytestmark = pytest.mark.anyio`.


@pytest.fixture
def anyio_backend():
    return "asyncio"
