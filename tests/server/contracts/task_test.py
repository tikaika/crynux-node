import secrets
from typing import Tuple

from anyio import create_task_group, create_memory_object_stream
import pytest
from web3 import Web3

from h_server.contracts import Contracts, TxRevertedError
from h_server.watcher import EventWatcher


@pytest.fixture(scope="module")
async def contracts_for_task(
    contracts_with_tokens: Tuple[Contracts, Contracts, Contracts], tx_option
):
    c1, c2, c3 = contracts_with_tokens

    node_contract_address = c1.node_contract.address
    task_contract_address = c1.task_contract.address
    task_amount = Web3.to_wei(400, "ether")
    if (await c1.token_contract.allowance(task_contract_address)) < task_amount:
        await c1.token_contract.approve(task_contract_address, task_amount, option=tx_option)
    if (await c2.token_contract.allowance(task_contract_address)) < task_amount:
        await c2.token_contract.approve(task_contract_address, task_amount, option=tx_option)
    if (await c3.token_contract.allowance(task_contract_address)) < task_amount:
        await c3.token_contract.approve(task_contract_address, task_amount, option=tx_option)

    node_amount = Web3.to_wei(400, "ether")
    if (await c1.token_contract.allowance(node_contract_address)) < node_amount:
        await c1.token_contract.approve(node_contract_address, node_amount, option=tx_option)
    if (await c2.token_contract.allowance(node_contract_address)) < node_amount:
        await c2.token_contract.approve(node_contract_address, node_amount, option=tx_option)
    if (await c3.token_contract.allowance(node_contract_address)) < node_amount:
        await c3.token_contract.approve(node_contract_address, node_amount, option=tx_option)
    
    try:
        await c1.node_contract.join(option=tx_option)
    except TxRevertedError as e:
        pass
    try:
        await c2.node_contract.join(option=tx_option)
    except TxRevertedError as e:
        pass
    try:
        await c3.node_contract.join(option=tx_option)
    except TxRevertedError as e:
        pass
    try:
        yield c1, c2, c3
    finally:
        await c1.node_contract.quit(option=tx_option)
        await c2.node_contract.quit(option=tx_option)
        await c3.node_contract.quit(option=tx_option)


async def test_task(contracts_for_task: Tuple[Contracts, Contracts, Contracts], tx_option):
    c1, c2, c3 = contracts_for_task

    task_hash = Web3.keccak(text="task_hash")
    data_hash = Web3.keccak(text="data_hash")

    receipt = await c1.task_contract.create_task(task_hash, data_hash, option=tx_option)

    events = await c1.task_contract.get_events(
        "TaskCreated",
        from_block=receipt["blockNumber"],
    )
    assert len(events) == 3
    event = events[0]
    task_id = event["args"]["taskId"]
    assert event["args"]["creator"] == c1.account
    assert event["args"]["taskHash"] == task_hash
    assert event["args"]["dataHash"] == data_hash

    selected_nodes = [event["args"]["selectedNode"] for event in events]
    assert all(c.account in selected_nodes for c in contracts_for_task)

    round_map = {
        event["args"]["selectedNode"]: event["args"]["round"] for event in events
    }

    result = bytes.fromhex("0102030405060708")

    for c in contracts_for_task:
        nonce = secrets.token_bytes(32)
        commitment = Web3.solidity_keccak(["bytes", "bytes32"], [result, nonce])

        receipt = await c.task_contract.submit_task_result_commitment(
            task_id, round_map[c.account], commitment, nonce, option=tx_option
        )
    events = await c1.task_contract.get_events(
        "TaskResultCommitmentsReady", from_block=receipt["blockNumber"]
    )
    assert len(events) == 1
    event = events[0]
    assert event["args"]["taskId"] == task_id

    from_block = receipt["blockNumber"]
    for c in contracts_for_task:
        receipt = await c.task_contract.disclose_task_result(
            task_id=task_id,
            round=round_map[c.account],
            result=result,
            option=tx_option
        )
    to_block = receipt["blockNumber"]
    events = await c1.task_contract.get_events(
        "TaskSuccess", from_block=from_block, to_block=to_block
    )
    assert len(events) == 1
    event = events[0]
    assert event["args"]["taskId"] == task_id
    assert event["args"]["result"] == result


async def test_task_with_event_watcher(
    contracts_for_task: Tuple[Contracts, Contracts, Contracts], tx_option
):
    c1, c2, c3 = contracts_for_task

    watcher = EventWatcher.from_contracts(c1)
    event_send, event_recv = create_memory_object_stream()

    async def _push_event(event):
        await event_send.send(event)

    async with create_task_group() as tg:
        tg.start_soon(watcher.start)

        task_hash = Web3.keccak(text="task_hash")
        data_hash = Web3.keccak(text="data_hash")

        watcher.watch_event(
            "task", "TaskCreated", _push_event, filter_args={"creator": c1.account}
        )
        await c1.task_contract.create_task(task_hash, data_hash, option=tx_option)

        task_id: int = -1
        selected_nodes = []
        round_map = {}
        for _ in range(3):
            event = await event_recv.receive()
            if task_id == -1:
                task_id = event["args"]["taskId"]
            else:
                assert task_id == event["args"]["taskId"]
            assert event["args"]["creator"] == c1.account
            assert event["args"]["taskHash"] == task_hash
            assert event["args"]["dataHash"] == data_hash

            selected_node = event["args"]["selectedNode"]
            round = event["args"]["round"]
            selected_nodes.append(event["args"]["selectedNode"])
            round_map[selected_node] = round

        assert all(c.account in selected_nodes for c in contracts_for_task)

        result = bytes.fromhex("0102030405060708")

        watcher.watch_event("task", "TaskResultCommitmentsReady", _push_event, filter_args={"taskId": task_id})
        for c in contracts_for_task:
            nonce = secrets.token_bytes(32)
            commitment = Web3.solidity_keccak(["bytes", "bytes32"], [result, nonce])

            await c.task_contract.submit_task_result_commitment(
                task_id, round_map[c.account], commitment, nonce, option=tx_option
            )

        event = await event_recv.receive()
        assert event["args"]["taskId"] == task_id

        watcher.watch_event("task", "TaskSuccess", _push_event, filter_args={"taskId": task_id})
        for c in contracts_for_task:
            await c.task_contract.disclose_task_result(
                task_id=task_id,
                round=round_map[c.account],
                result=result,
                option=tx_option
            )

        event = await event_recv.receive()
        assert event["args"]["taskId"] == task_id
        assert event["args"]["result"] == result

        await event_recv.aclose()
        await event_send.aclose()
        watcher.stop()
