"""Workspace initialization command."""

from rich.console import Console

from ..agent.ui_event_console import UiEventConsole
from ..context import ContextBuilder, Conversation
from ..core import load_config
from ..llm import create_agent_client
from ..llm.schema import Message
from ..tools import (
    ToolRegistry,
    ShellExecutor,
    READ_FILE_DEFINITION,
    WRITE_FILE_DEFINITION,
    EDIT_FILE_DEFINITION,
    EXECUTE_SHELL_DEFINITION,
    create_read_file,
    create_write_file,
    create_edit_file,
    create_execute_shell,
)
from ..tui.events import (
    AssistantTextEvent,
    ErrorEvent,
    ResumeHistoryEvent,
    ToolCallEvent,
    ToolResultEvent,
    WarningEvent,
)
from ..worker.runner import run_simple_tool_loop
from ..workspace import WorkspaceManager, WorkspaceInitializer


class _RichUiSink:
    """Minimal Rich renderer for the non-Textual initialization flow."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def emit(self, event: object) -> None:
        match event:
            case AssistantTextEvent(content=content):
                self._console.print(content)
            case ToolCallEvent(name=name, summary=summary):
                self._console.print(f"  {name}: {summary}", style="blue", markup=False)
            case ToolResultEvent(name=name, summary=summary, failed=failed):
                self._console.print(
                    f"    {name}: {summary}",
                    style="red" if failed else "dim",
                    markup=False,
                )
            case WarningEvent(message=message):
                self._console.print(f"Warning: {message}", style="yellow", markup=False)
            case ErrorEvent(message=message):
                self._console.print(f"Error: {message}", style="red", markup=False)
            case ResumeHistoryEvent(summary=summary):
                self._console.print(summary, markup=False)


def _setup_tools(config) -> ToolRegistry:
    """Set up tools for init agent."""
    registry = ToolRegistry()
    agent_os_dir = config.get_agent_os_dir()
    allowed_paths = [str(agent_os_dir)]
    tools_config = config.tools

    executor = ShellExecutor(
        agent_os_dir=agent_os_dir,
        blacklist=tools_config.shell.blacklist,
        timeout=tools_config.shell.timeout,
    )
    registry.register(
        "execute_shell",
        create_execute_shell(executor),
        EXECUTE_SHELL_DEFINITION,
    )
    registry.register(
        "read_file",
        create_read_file(allowed_paths, agent_os_dir),
        READ_FILE_DEFINITION,
    )
    registry.register(
        "write_file",
        create_write_file(allowed_paths, agent_os_dir),
        WRITE_FILE_DEFINITION,
    )
    registry.register(
        "edit_file",
        create_edit_file(allowed_paths, agent_os_dir),
        EDIT_FILE_DEFINITION,
    )
    return registry


def _run_init_agent(config, workspace: WorkspaceManager) -> None:
    """Run init agent conversation to set up persona."""
    if "init" not in config.agents:
        raise ValueError("agents.init not configured. Add it to config to use init agent.")

    init_agent_config = config.agents["init"]
    client = create_agent_client(init_agent_config, retry_label="init")
    system_prompt = workspace.get_system_prompt("init")

    prompt_console = Console()
    console = UiEventConsole(_RichUiSink(prompt_console), show_tool_use=True)
    conversation = Conversation()
    builder = ContextBuilder(system_prompt=system_prompt)
    registry = _setup_tools(config)

    console.print_info("Starting persona setup. Type /exit to exit.\n")

    while True:
        try:
            user_input = prompt_console.input("> ")
        except (EOFError, KeyboardInterrupt):
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() in ("/exit", "/quit", "/q"):
            break

        conversation.add("user", user_input)
        try:
            def _handle_tool_calls(response) -> None:
                conversation.add_assistant_with_tools(
                    response.content,
                    response.tool_calls,
                    reasoning_content=response.reasoning_content,
                    reasoning_details=response.reasoning_details,
                )
                for tool_call in response.tool_calls:
                    console.print_tool_call(tool_call)
                    result = registry.execute(tool_call)
                    console.print_tool_result(tool_call, result.content)
                    conversation.add_tool_result(
                        tool_call.id,
                        tool_call.name,
                        result.content,
                    )

            final_content = run_simple_tool_loop(
                client,
                build_messages=lambda: builder.build(conversation),
                tool_definitions=registry.get_definitions(),
                handle_tool_calls=_handle_tool_calls,
                finalization_messages=lambda: [
                    *builder.build(conversation),
                    Message(
                        role="user",
                        content=(
                            "FINALIZATION STEP: provide the final user-facing reply now. "
                            "Do not call tools."
                        ),
                    ),
                ],
            )
            conversation.add("assistant", final_content)
            console.print_assistant(final_content)
        except Exception as e:
            console.print_error(str(e))
            conversation.truncate_to(max(len(conversation) - 1, 0))

    console.print_info("Persona setup complete.")


def init_command() -> None:
    """Initialize workspace directory and run init agent."""
    console = Console()

    config = load_config()
    agent_os_dir = config.get_agent_os_dir()

    from ..timezone_utils import configure_runtime_timezone
    configure_runtime_timezone(config.app.timezone)

    console.print(f"[blue]Initializing workspace at:[/blue] {agent_os_dir}")

    manager = WorkspaceManager(agent_os_dir)
    initializer = WorkspaceInitializer(manager)

    if manager.is_initialized():
        version = manager.get_kernel_version()
        console.print(f"[yellow]Workspace already initialized (v{version})[/yellow]")

        if initializer.needs_upgrade():
            console.print("[yellow]Kernel upgrade available[/yellow]")
            if _confirm(console, "Upgrade kernel? (memory will be preserved)"):
                initializer.upgrade_kernel()
                console.print("[green]Kernel upgraded successfully[/green]")

        if "init" in config.agents and _confirm(console, "Re-run persona setup?"):
            _run_init_agent(config, manager)
        return

    initializer.create_structure()
    console.print("[green]Workspace created successfully[/green]\n")

    if "init" in config.agents:
        _run_init_agent(config, manager)
    else:
        console.print("[yellow]agents.init not configured, skipping persona setup.[/yellow]")
        console.print("[dim]Add agents.init to config to enable guided persona setup.[/dim]")


def _confirm(console: Console, message: str) -> bool:
    """Ask for confirmation."""
    response = console.input(f"{message} [y/N] ")
    return response.lower() in ("y", "yes")
