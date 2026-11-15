from hexbytes import HexBytes
from pathlib import Path
import pytest

from blockchain.database import SQLite
from blockchain.sync import EventDeposit, EventWithdraw
from util.mytype import ChainID, Symbol, TornadoUnit
import config


def test_event_counts_use_persisted_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, symbol, unit = ChainID.SEPOLIA, Symbol.ETH, TornadoUnit.UNIT_0_1
    monkeypatch.setitem(config.DATABASE_PATHS[chain][symbol], unit, str(tmp_path / 'events.sqlite'))
    db: SQLite = SQLite()
    try:
        assert db.open(chain, symbol, unit)
        assert db.get_event_counts() == (0, 0)
        deposit   : EventDeposit  = EventDeposit(1, 1, HexBytes('01' * 32), HexBytes('02' * 32), 0)
        withdrawal: EventWithdraw = EventWithdraw(2, HexBytes('03' * 32), HexBytes('04' * 32), '0x' + '00' * 20, 0)
        assert db.add_synchronized(2, [deposit], [withdrawal])
        assert db.get_event_counts() == (1, 1)
        # Re-reading a block must not inflate the UI's historical totals.
        assert db.add_synchronized(2, [deposit], [withdrawal])
        assert db.get_event_counts() == (1, 1)
    finally:
        db.close()


def test_sync_rejects_a_refused_write_without_advancing_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain : ChainID     = ChainID.SEPOLIA
    symbol: Symbol      = Symbol.ETH
    unit  : TornadoUnit = TornadoUnit.UNIT_0_1
    monkeypatch.setitem(config.DATABASE_PATHS[chain][symbol], unit, str(tmp_path / 'events.sqlite'))
    db: SQLite = SQLite()
    try:
        assert db.open(chain, symbol, unit)
        checkpoint: int | None = db.get_latest_block_number()
        assert checkpoint is not None
        with monkeypatch.context() as refused:
            refused.setattr(db.taskq, 'run_sync', lambda job: False)
            assert not db.add_synchronized(checkpoint + 1, [], [])
        assert db.get_latest_block_number() == checkpoint
    finally:
        db.close()
