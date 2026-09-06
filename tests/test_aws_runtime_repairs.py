"""Regression evidence for reviewed AWS overlay repairs; no exchange writes."""
from decimal import Decimal
import json
import sqlite3
import time

import pytest

from test_execution_safety import make_live_adapter, make_store, seed_protected_trade
from services.common.models import LifecycleState, ProtectionMode
from services.telegram_broker import stable_panel as panel


def auto_fixture(tmp_path, monkeypatch, price='100.20', bullish=True):
    monkeypatch.setenv('AUTO_PROTECTION_ENABLED', 'true')
    monkeypatch.setenv('AUTO_BREAK_EVEN_TRIGGER_PCT', '20')
    monkeypatch.setenv('FEE_PCT_PER_SIDE', '0.1')
    adapter, store, guard, controller, gateway, _ = make_live_adapter(tmp_path)
    seed_protected_trade(store)
    store.data['last_reconciliation_status'] = 'RECONCILED'
    gateway.ticker_price = lambda symbol: {'symbol': symbol, 'price': price}
    flow = {'ok': True, 'pair_state_hash': controller.load()['state_hash'],
            'generated_at_epoch': time.time(), 'classification': {'bullish': bullish}}
    return adapter, store, guard, gateway, flow


@pytest.mark.parametrize('price,bullish', [('99', True), ('100.2', True), ('110', False), ('110', 'false')])
def test_adaptive_keeps_oco_until_fresh_bullish_fee_margin(tmp_path, monkeypatch, price, bullish):
    adapter, store, _, gateway, flow = auto_fixture(tmp_path, monkeypatch, price, bullish)
    adapter.maybe_auto_manage(flow)
    assert gateway.cancel_calls == gateway.place_calls == 0
    assert store.trade('trade-1')['protection_mode'] == 'OCO_TRAILING'


@pytest.mark.parametrize('defect', ['owner-off', 'stale', 'unresolved', 'reconciliation', 'account', 'stream'])
def test_adaptive_guards_leave_exchange_protection_untouched(tmp_path, monkeypatch, defect):
    adapter, store, _, gateway, flow = auto_fixture(tmp_path, monkeypatch, '110')
    if defect == 'owner-off': store.data['auto_protection_enabled'] = False
    if defect == 'stale': flow['generated_at_epoch'] -= 500
    if defect == 'unresolved':
        store.prepare_intent(intent_id='pending', operation='PROTECTION', trade_id='trade-1',
                             symbol='BTCUSDT', endpoint='order', request={})
    if defect == 'reconciliation': store.data['last_reconciliation_status'] = 'RECONCILIATION_FAILED'
    if defect == 'account': adapter._on_account_update({})
    if defect == 'stream': adapter.stream = object()
    adapter.maybe_auto_manage(flow)
    assert gateway.cancel_calls == gateway.place_calls == 0


def test_invalid_conversion_does_not_pause_entries_or_risk(tmp_path):
    adapter, store, guard, _, gateway, _ = make_live_adapter(tmp_path)
    seed_protected_trade(store)
    store.set_entries(True)
    adapter.enabled = True
    gateway.ticker_price = lambda symbol: {'symbol': symbol, 'price': '99'}
    ok, _ = adapter.convert('BTCUSDT', ProtectionMode.FIXED_OCO, break_even=True)
    assert not ok and store.entries() and adapter.enabled
    assert not guard.pauses and gateway.cancel_calls == 0


def test_break_even_sell_limit_includes_fill_buffer(tmp_path):
    adapter, store, _, _, gateway, validator = make_live_adapter(tmp_path)
    seed_protected_trade(store)
    ok, _ = adapter.convert('BTCUSDT', ProtectionMode.FIXED_OCO, break_even=True)
    assert ok
    from services.execution_sidecar.protection_modes import fee_adjusted_break_even
    floor = fee_adjusted_break_even(Decimal('100'),
        buy_fee_pct=adapter.factory.settings.fee_pct_per_side,
        sell_fee_pct=adapter.factory.settings.fee_pct_per_side, slippage_pct=Decimal('.05'))
    assert Decimal(validator.calls[-1][2]['belowPrice']) >= floor


@pytest.mark.parametrize('owner_enabled,risk_paused', [(True, False), (False, False), (True, True)])
def test_auto_resume_preserves_owner_and_risk_pauses(tmp_path, monkeypatch, owner_enabled, risk_paused):
    adapter, store, guard, _, flow = auto_fixture(tmp_path, monkeypatch, '110')
    adapter.enabled = owner_enabled
    store.set_entries(owner_enabled)
    guard.state = {'global_pause': 'risk-limit' if risk_paused else ''}
    def converted(*args, **kwargs):
        adapter.enabled = False
        store.set_entries(False, 'protection-conversion-in-progress')
        return True, 'converted'
    monkeypatch.setattr(adapter, 'convert', converted)
    monkeypatch.setattr(adapter, 'verified_reconcile', lambda: {'ok': True})
    adapter.maybe_auto_manage(flow)
    assert store.entries() == (owner_enabled and not risk_paused)
    assert guard.state['global_pause'] == ('risk-limit' if risk_paused else '')
    assert guard.clears == 0


@pytest.mark.parametrize('executed,client,status,accepted', [
    ('0.0100','exit-1','FILLED',True), ('0.0099','exit-1','FILLED',False),
    ('0.0100','other','FILLED',False), ('0.0100','exit-1','CANCELED',False)])
def test_emergency_recovery_requires_exact_authenticated_full_fill(tmp_path, executed, client, status, accepted):
    adapter, store, _, _, gateway, _ = make_live_adapter(tmp_path)
    seed_protected_trade(store)
    store.prepare_intent(intent_id='exit', operation='EMERGENCY_EXIT', trade_id='trade-1',
        symbol='BTCUSDT', endpoint='order', request={'quantity':'0.0100','newClientOrderId':'exit-1'})
    store.finish_intent('exit','CONFIRMED',exchange_order_id=9000)
    gateway.order_lookup[9000] = {'orderId':9000, 'symbol':'BTCUSDT', 'side':'SELL',
        'clientOrderId':client,'status':status, 'executedQty':executed, 'origQty':'0.0100',
        'cummulativeQuoteQty':'1.10', 'type':'MARKET', 'updateTime':int(time.time()*1000)}
    result = adapter._reconcile_confirmed_emergency_exits()
    assert result['ok'] is accepted
    assert (store.trade('trade-1')['lifecycle_state'] == LifecycleState.EXIT_FILLED.value) is accepted


def test_idle_wal_anchor_allows_reader_and_full_checkpoint(tmp_path):
    store = make_store(tmp_path)
    anchor = store.hold_wal_open()
    try:
        for i in range(10): store.upsert_trade(str(i), 'BTC/USDT')
        with sqlite3.connect(store.db_path) as writer:
            assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        assert not anchor.in_transaction
        with sqlite3.connect(store.db_path.as_uri()+'?mode=ro', uri=True) as reader:
            assert reader.execute('SELECT count(*) FROM trade_records').fetchone()[0] == 10
    finally:
        anchor.close()


def test_notifier_delivers_beyond_first_500_without_replaying_acknowledged_rows(tmp_path, monkeypatch):
    source = tmp_path/'source.sqlite'
    with sqlite3.connect(source) as con:
        con.execute('CREATE TABLE processed_signals(signal_id TEXT,pair TEXT,candle_time TEXT,result TEXT,processed_at TEXT)')
        con.executemany('INSERT INTO processed_signals VALUES(?,?,?,?,?)',
            [(f'{i:04d}','BTC/USDT','2026-09-06','accepted','2026-09-06') for i in range(601)])
    monkeypatch.setattr(panel, 'SIGNAL_NOTIFY_DB', tmp_path/'notify.sqlite')
    notify = panel._notifier_db()
    notify.execute("INSERT INTO notifier_meta VALUES('bootstrapped','1')")
    notify.executemany('INSERT INTO notified_signals VALUES(?,?,?)',
                      [(f'{i:04d}',1,'accepted') for i in range(500)])
    notify.commit(); notify.close()
    def db():
        con = sqlite3.connect(source); con.row_factory = sqlite3.Row; return con
    monkeypatch.setattr(panel, 'db', db)
    monkeypatch.setattr(panel, '_signal_notification_text', lambda row: row['signal_id'])
    sent = []
    monkeypatch.setattr(panel, 'send_fixed', lambda text, owner: sent.append(text))
    monkeypatch.setattr(panel.base, 'audit', lambda *a, **kw: None)
    class StopLoop(BaseException): pass
    cycles = []
    def sleep(_):
        cycles.append(1)
        if len(cycles) == 4: raise StopLoop()
    monkeypatch.setattr(panel.time, 'sleep', sleep)
    with pytest.raises(StopLoop): panel.signal_notifier_loop()
    assert sent == [f'{i:04d}' for i in range(500,601)]


def test_telegram_negative_ack_is_not_recorded_as_delivery(monkeypatch):
    monkeypatch.setattr(panel.base, 'TOKEN', 'test-token')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'ok':False}
    monkeypatch.setattr(panel.requests, 'post', lambda *a,**kw: Response())
    with pytest.raises(RuntimeError, match='acknowledge'): panel.send_fixed('test')


def test_testnet_panel_refuses_live_start(monkeypatch):
    monkeypatch.setenv('EXECUTION_MODE','live')
    with pytest.raises(RuntimeError,match='Testnet'): panel.main()


def test_bullish_profit_skips_intermediate_break_even_cancel(tmp_path, monkeypatch):
    adapter, _, _, _, flow = auto_fixture(tmp_path, monkeypatch, '110')
    monkeypatch.setenv('AUTO_BREAK_EVEN_TRIGGER_PCT', '0.5')
    converted=[]
    monkeypatch.setattr(adapter, '_auto_convert', lambda symbol,mode,**kw: converted.append(mode))
    adapter.maybe_auto_manage(flow)
    assert converted == [ProtectionMode.TRAILING_ONLY]


def test_health_does_not_report_a_stale_ok_snapshot_as_ready(monkeypatch):
    monkeypatch.setattr(panel.base, 'read_json', lambda *args: {
        'ts':time.time()-600,'ok':True,'reconciliation_ok':True,'user_stream_ok':True,'moneyflow_ok':True})
    monkeypatch.setattr(panel.base, 'ft_call', lambda *args: {'ok':False})
    result=panel.health_text()
    assert 'Sidecar: NOT READY' in result and 'Snapshot current: False' in result


def test_gross_exit_combines_partial_and_emergency_without_duplicate_fees(tmp_path):
    store=make_store(tmp_path)
    trade={'trade_id':'pnl','pair':'BTC/USDC','take_profit_order_id':8,'stop_order_id':9,
           'average_entry_price':'100','filled_quantity':'1'}
    store.prepare_intent(intent_id='emergency',operation='EMERGENCY_EXIT',trade_id='pnl',
                         symbol='BTCUSDC',endpoint='order',request={})
    store.finish_intent('emergency','CONFIRMED',exchange_order_id=10)
    with sqlite3.connect(store.db_path) as con:
        # Real schema; REST and stream copies use different event identities.
        for i,(oid,qty,proceeds) in enumerate([(9,'.4','44'),(9,'.4','44'),(10,'.6','66')]):
            event={'e':'executionReport','E':1788700000000+i,'s':'BTCUSDC','S':'SELL',
                   'i':oid,'z':qty,'Z':proceeds,'X':'FILLED' if oid==10 else 'CANCELED',
                   'n':'.1','N':'USDC','I':i}
            store.record_exchange_event(event)
        con.row_factory=sqlite3.Row
        result=panel.exit_result(con,trade)
    assert result['exit_qty']==1 and result['pnl']==10
    assert result['quote_asset']=='USDC' and result['fee_basis']=='gross; fees excluded'


def test_account_event_coalescing_still_detects_unknown_btc(tmp_path):
    adapter, store, guard, _, gateway, _=make_live_adapter(tmp_path)
    store.set_entries(True)
    adapter.enabled=True
    adapter._on_account_update({'e':'outboundAccountPosition'})
    first=adapter._account_dirty_since
    adapter._on_account_update({'e':'balanceUpdate'})
    assert adapter._account_dirty_since==first and store.entries()
    adapter._account_dirty_since-=6
    adapter.tick()
    assert not store.entries() and guard.pauses[-1]=='reconciliation-failed'
    assert gateway.place_calls==gateway.cancel_calls==0
