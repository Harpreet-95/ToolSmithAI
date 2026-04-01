from src.interpreter.task_interpreter import interpret_task
from src.output.output_formatter import format_output

def handle_input(user_input: str) -> dict:
    interpreted = interpret_task(user_input)
    return format_output(interpreted)
