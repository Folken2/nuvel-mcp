"""Tasks (``io.modelcontextprotocol/tasks``): a tool call that outlives its request.

``store``   the task records: files under the feedback store, or Postgres.
``runner``  the in-process workers that run task tools, with cooperative
            cancellation and a heartbeat so a task orphaned by a restart is
            reported as failed instead of working forever.

A task's result is the tool's output, so it is user content: it is kept only
until the task's TTL (``NUVEL_MCP_TASK_TTL``, default 24 hours), returned only
to the tenant that created it, and never logged or relayed. The protocol side
is :mod:`nuvel_mcp.extensions.tasks`.
"""
