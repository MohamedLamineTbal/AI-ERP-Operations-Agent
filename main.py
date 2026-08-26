"""Minimal terminal entry point with LangGraph human approval."""

from graph import (
    build_erp_graph,
    create_thread_config,
    get_interrupt_payload,
    resume_erp_graph,
    run_erp_graph,
)


def _ask_for_approval() -> bool:
    while True:
        answer = input("Approve? [y/n] ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter y or n.")


def main() -> None:
    question = input("Question: ").strip()
    if not question:
        print("Please enter a question about the ERP database.")
        return

    try:
        graph = build_erp_graph()
        config = create_thread_config()
        result = run_erp_graph(question, graph=graph, config=config)

        approval = get_interrupt_payload(result)
        if approval:
            actions = approval.get("actions") or [approval]
            heading = "Proposed action:" if len(actions) == 1 else "Proposed actions:"
            print(f"\n{heading}")
            for index, action in enumerate(actions, start=1):
                if len(actions) > 1:
                    print(f"\n{index}.")
                print(f"{action['tool_name']}({action['arguments']})")
                print(f"Reason: {action['reason']}")
                print(f"What will change: {action['what_will_change']}")
            result = resume_erp_graph(graph, config, _ask_for_approval())
    except (RuntimeError, ValueError) as error:
        print(f"Error: {error}")
        return

    print(f"\n{result['answer']}")


if __name__ == "__main__":
    main()
