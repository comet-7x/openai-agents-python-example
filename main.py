import argparse
import asyncio
import uuid

from agents.extensions.sandbox.modal import ModalSandboxClient, ModalSandboxClientOptions

from orchestrator import OrchestratorAgent
from orchestrator import OrchestratorHooks

async def main(prompt: str | None, interactive: bool = False):
    trace_id = "trace_" + str(uuid.uuid4())
    model = "gpt-5.4"

    modal_sandbox_client = ModalSandboxClient()
    modal_sandbox_client_options = ModalSandboxClientOptions(
        app_name="oai-sb-example",
        workspace_persistence="snapshot_filesystem",
        gpu="a10",
    )

    orchestrator = OrchestratorAgent(
        model=model,
        sandbox_client=modal_sandbox_client,
        sandbox_options=modal_sandbox_client_options,
        trace_id=trace_id,
    )

    magenta = "\033[35m"
    reset = "\033[0m"
    print(
        f"\nView trace on OpenAI:\n{magenta}https://platform.openai.com/logs/trace?trace_id={trace_id}{reset}\n"
    )

    try:
        if prompt is not None:
            output = await orchestrator.run(prompt)
            print(output)
            return

        if interactive:
            while True:
                user_input = input("> ")
                if user_input.lower() == "exit":
                    return
                if not user_input.strip():
                    continue
                output = await orchestrator.run(user_input)
                print(output)
            return

        print("No prompt provided. Use --prompt \"<prompt>\" or --interactive to run.")
        return

    finally:
        await orchestrator.delete_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=str, default=None, help="The prompt to send to the orchestrator.")
    parser.add_argument("--interactive", action="store_true", default=False, help="Run in interactive mode.")
    args = parser.parse_args()
    asyncio.run(main(args.prompt, args.interactive))
