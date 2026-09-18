# Scripted-wire test harness

The scripted-wire harnesses should share one setup and teardown path. Keep the
wire script, observed server-tool activity, and deadline assertions in the
fixture so a test expresses protocol behavior rather than duplicating
transport plumbing.

When consolidating a harness, preserve tests for empty completion, server-tool
activity, and a deadline that covers all reissues. Run the focused example
tests and the complete `pytest` suite.
