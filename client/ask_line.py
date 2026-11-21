"""One question at the terminal for connect.py, readable everywhere: the question on its own line
(connect.ps1 pipes connect.py's output through Out-Host, which shows a prompt only once its line
ends), then the answer after "> ". Answers come from stdin only, never argv or the environment."""


def ask_line(question: str, ask=input) -> str:
    print(question, flush=True)
    return ask("> ").strip()
