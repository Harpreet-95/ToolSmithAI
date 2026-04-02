# ToolSmithAI

## Overview
ToolSmithAI is a simple task interpreter that converts natural language input into structured task instructions.

It identifies:
- The main task (e.g., send email, generate report)
- The frequency (daily, weekly, monthly)

## Features
- Keyword-based task detection
- Priority-based intent resolution for multi-intent inputs
- Frequency extraction (daily, weekly, monthly)
- Handles unknown or unsupported inputs safely

## Example

Input:

Email me a weekly report

Output:
```
{
  "original_input": "Email me a weekly report",
  "task_type": "generate_report",
  "frequency": "weekly"
}
```
## More Examples

Input:

Send a weekly email update

Output:
```
{
  "original_input": "Send a weekly email update",
  "task_type": "send_email",
  "frequency": "weekly"
}
```

Input:

Do something random

Output:
```
{
  "original_input": "Do something random",
  "task_type": "unknown",
  "frequency": null
}
```
## Architecture

ToolSmithAI follows a simple pipeline:

User Input  
→ Normalize (lowercase)  
→ Detect all matching keywords  
→ Apply priority rules  
→ Extract frequency  
→ Return structured output  

### Flow

Input → Detection → Priority Resolution → Output

## How It Works
1. Converts user input to lowercase
2. Detects all matching task keywords
3. Applies priority rules to resolve conflicts
4. Extracts frequency if present
5. Returns structured output

## Limitations
- Relies on simple keyword matching
- May not handle complex or ambiguous natural language
- Limited to predefined task types

## Future Improvements
- Use NLP models instead of keyword matching
- Support more task types
- Improve handling of ambiguous inputs

## Author
Built as part of an AI + Data Engineering learning project.
