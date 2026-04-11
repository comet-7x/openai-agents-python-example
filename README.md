# Parallel GPU Coding Agents
This repo is an example of using OpenAI Agents SDK and Modal Sandboxes to implement general purpose coding agent harness with async workers. 

### Example: Parameter Golf
While implemented to be general-purpose, the `/skills` markdown plugin system allows agents to opt-in to guides and context to improve their performance.

In this example we use OpenAI's [Parameter Golf](https://openai.com/index/parameter-golf/) challenge, with an associated skill, to allow the orchestrator agent to command a fleet of on-GPU workers to run parameter optimization experiments in parallel.

To run it with a one-shot task:
```bash
uv run main.py --prompt "Start working on the parameter golf challenge. First pull the repo, exploring past approaches, and create a working environment. Then create a plan for five parallel approaches that could be done, and guide those experiments through to completion, reporting their performance back."
```

This should trigger the loading of the (parameter_golf.md)[/skills/parameter_golf.md] skill, and kick off parallel experiments.

The harness can also be run as an interactive chat with:
```bash
uv run main.py --interactive
```

## Architecture

### Orchestrator Agent
The orchestrator agent is the primary chat agent equipped with a `Session` memory, subagents-as-tools, a skills subsystem and Subagent pool subsystem. 

### Subagent Pool
The orchestrator owns a pool of subagents, running as asyncio tasks to allow the orchestrator to delegate messages. The Pool enforces a strict GPU limit to ensure the orchestrator does not explode GPU budget on unproven tasks. Individual GPU type limits are configurable.

### Subagents
The subagents are your "coding agents" with shell and file tools. They are implemented as tools to the orchestrator agent, allowing implementation details for each experiment to accumulate in the subagent and not bloat the orchestrator's context window. 

Each subagent has their own `Session` working memory and attached `ModalSandboxSession`. A subagent is a 1:1 map to a running sandbox.