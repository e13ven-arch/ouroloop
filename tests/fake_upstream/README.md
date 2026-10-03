A miniature stand-in for zapier/AutomationBench, so the adapter's tests never need the real one.

It copies the parts the adapter binds to, and nothing else: a pydantic-free `WorldState` with the methods the
adapter calls, the rubric's free-assertion semantics, an `api` toolset of three functions over a two-service
world, upstream's task-row shape (`info` as a JSON string, `task_name` inside it), and a task-contract hash. Two
tasks, one of them an inverse task whose correct action is to leave the world alone.

Where a test needs to know that the adapter matches the real upstream rather than this one, that is what
`adapters.automationbench.contract_check` is for, and it runs against whatever checkout is configured.
