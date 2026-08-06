"""The claude-code autocode adapter must feed the prompt on STDIN, never argv.

A large prompt (card + codebase context + prior-round feedback) passed inline on
`docker run ... claude -p <prompt>` blows past the OS ARG_MAX and kills the build
mid-run with OSError [Errno 7] "Argument list too long: docker". The prompt must
ride on stdin instead (mirrors the opencode adapter). This locks that.
"""

from skharness.autocode.adapters.claude_code import ClaudeCodeAdapter

_BIG_PROMPT = "x" * 500_000  # far larger than any real prompt, well past ARG_MAX


def _adapter():
    return ClaudeCodeAdapter(allowed_tools=["Bash", "Edit"], max_turns=6, model="sonnet")


def test_prompt_is_not_a_positional_argv_element():
    a = _adapter()
    for light in (False, True):
        argv = a._argv(_BIG_PROMPT, light=light)
        assert _BIG_PROMPT not in argv, "prompt must not ride on argv (ARG_MAX)"
        # -p stays, as a bare print-mode flag that reads the prompt from stdin.
        assert argv[:2] == ["claude", "-p"]


def test_stdin_carries_the_prompt():
    a = _adapter()
    assert a._stdin_for(_BIG_PROMPT) == _BIG_PROMPT
