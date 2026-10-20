import { connectWalletConnect } from './wallet.js';

const $ = (id) => document.getElementById(id);
const number = (value) => value == null ? '—' : Number(value).toLocaleString('en-US');
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const formatTotals = (totals) => [...totals].map(([symbol, amount]) => `${Number(amount.toPrecision(12)).toLocaleString('en-US', { maximumFractionDigits: 18 })} ${symbol.toUpperCase()}`).join(' + ') || '—';
const pending = new Map();
let socket, sequence = 0, snapshot = { instances: [] };
let selected = '', poolList = '', account = '', walletChain = '', busy = false, draft = null;
let lastLogId = 0, firstLogs = true;
let provider;
const providers = new WeakSet();
let depositGroups = [], withdrawalResults = [], progressPrefix = '';
let balanceView;
const relayerJobs = new Map();
const walletBatches = new Map();
const singleWithdrawals = new Map();
const withdrawalTexts = { single: '', batch: [''] };
const states = {
    starting: 'Starting', connecting: 'Connecting RPC', retrying: 'Retrying RPC',
    initializing: 'Initializing', syncing: 'Syncing', ready: 'Ready', error: 'Error',
    stopped: 'Stopped', building_tree: 'Building tree',
    proving: 'Generating proof', relaying: 'Submitting to relayer', working: 'Working',
};
const stateLabel = (state) => states[state] || state?.replaceAll('_', ' ') || 'Starting';
const noteId = (note) => (note || '').trim().split('-').slice(0, 3).join('-').toLowerCase();
const isBatch = (name) => $(`${name}-batch`).getAttribute('aria-pressed') === 'true';
const withdrawalInputs = () => [...$('withdraw-notes').querySelectorAll('textarea')];
const withdrawalNotes = () => withdrawalInputs().map((input) => input.value.trim()).filter(Boolean);
const depositNotes = () => draft.batch ? draft.notes.map((item) => item.note) : [draft.note];

function checkBackend(id) {
    if (socket?.readyState !== WebSocket.OPEN) throw new Error('Backend disconnected. Please wait for reconnection.');
    if (snapshot.stopping) throw new Error('Backend is shutting down.');
    if (id !== undefined && !snapshot.instances.some((item) => item.id === id)) throw new Error(id
        ? 'This instance is not configured in the backend. Check the selected pool or note.'
        : 'No pool is available yet. Wait for backend status.');
}

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
}

function message(text, error = false) {
    if (text && progressPrefix) text = `${progressPrefix} · ${text}`;
    $('message').textContent = text;
    $('message').dataset.error = error;
    let target = $('message');
    for (const name of ['backup', 'wallet', 'backup-confirm', 'receipt']) {
        if (!$(`${name}-dialog`).open) continue;
        target = $(`${name}-message`);
        target.textContent = text;
        target.dataset.error = error;
    }
    if (error) target.scrollIntoView({ block: 'nearest' });
}

async function rpc(method, params, signal) {
    if ('note' in params && !params.note.trim()) throw new Error('Enter your note.');
    checkBackend(['status', 'receipt'].includes(method) ? undefined
        : params.instance || params.entries?.[0]?.instance || noteId(params.note || params.notes?.[0]));
    signal?.throwIfAborted();
    const id = ++sequence;
    let abort;
    try {
        return await new Promise((resolve, reject) => {
            abort = () => reject(signal.reason);
            signal?.addEventListener('abort', abort, { once: true });
            pending.set(id, { resolve, reject });
            socket.send(JSON.stringify({ id, method, params }));
        });
    } finally {
        pending.delete(id);
        signal?.removeEventListener('abort', abort);
    }
}

function connect() {
    const url = new URLSearchParams(location.search).get('ws') || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname || '127.0.0.1'}:8765`;
    try { socket = new WebSocket(url); }
    catch (error) { message(error.message, true); return; }
    firstLogs = true;
    socket.onopen = () => { renderStatus(); updateLogStatus(); };
    socket.onmessage = ({ data }) => {
        let result;
        try { result = JSON.parse(data); } catch { return; }
        if (result.type === 'logs') { appendLogs(result); return; }
        if (result.type === 'status') {
            snapshot = result;
            if (!snapshot.instances.some((item) => item.id === selected)) selected = snapshot.instances[0]?.id || '';
            const signature = JSON.stringify(snapshot.instances.map(({ id, chain_id, symbol, unit }) => [id, chain_id, symbol, unit]));
            if (signature !== poolList) { poolList = signature; renderPools(); }
            renderStatus();
            return;
        }
        const request = pending.get(result.id);
        if (!request) return;
        if (result.type === 'progress') {
            if (busy && typeof result.message === 'string') message(result.message);
            return;
        }
        pending.delete(result.id);
        if (result.error) request.reject(new Error(result.error));
        else request.resolve(result.result);
    };
    socket.onclose = () => {
        for (const request of pending.values()) {
            request.reject(new Error('Backend disconnected.'));
        }
        pending.clear();
        renderStatus();
        updateLogStatus();
        setTimeout(connect, 2000);
    };
    socket.onerror = () => socket.close();
}

function updateLogStatus() {
    $('logs-status').textContent = socket?.readyState === WebSocket.OPEN
        ? `${$('logs').childElementCount} / 500 lines · connected`
        : 'Disconnected · logs kept · reconnecting…';
}

function appendLogs({ entries, reset }) {
    const output = $('logs');
    if (reset || firstLogs) { output.replaceChildren(); lastLogId = 0; firstLogs = false; }
    for (const entry of entries) {
        if (entry.id <= lastLogId) continue;
        lastLogId = entry.id;
        const line = element('div', entry.text, 'log-line');
        line.dataset.level = entry.level;
        output.append(line);
    }
    while (output.childElementCount > 500) output.firstElementChild.remove();
    if ($('auto-scroll').checked) output.scrollTop = output.scrollHeight;
    updateLogStatus();
}

function hasProgress(item) {
    return socket?.readyState === WebSocket.OPEN
        && !['starting', 'connecting', 'retrying', 'initializing', 'error', 'stopped'].includes(item?.state)
        && ['syncing', 'ready'].includes(item?.sync_state || item?.state)
        && Number.isFinite(item?.progress) && item.latest_block != null;
}

const pool = () => snapshot.instances.find((item) => item.id === selected);

function options(select, values, value) {
    select.replaceChildren(...values.map(([key, label]) => new Option(label, key)));
    select.value = value;
}

function validateWithdrawalNote(note, requireSelectedNetwork = true) {
    if (!note.trim()) return {};
    const parts = note.trim().split('-');
    if (parts.length !== 5) return { error: 'Invalid note: expected 5 parts separated by hyphens.' };
    if (!parts.slice(3).every((part) => /^[0-9a-f]{62}$/i.test(part))) return { error: 'Invalid note: both secret fields must contain 62 hexadecimal characters.' };
    if (!snapshot.instances.length) return { error: 'Backend pool information is not available yet.' };
    const network = snapshot.instances.filter((item) => item.chain === parts[0].trim().toLowerCase());
    if (!network.length) return { error: 'Unknown network.' };
    const tokens = network.filter((item) => item.symbol === parts[1].toLowerCase());
    if (!tokens.length) return { error: 'Unknown token for this network.' };
    const instance = tokens.find((item) => item.unit === parts[2]);
    if (!instance) return { error: 'Unknown pool amount for this token.' };
    if (requireSelectedNetwork && instance.chain_id !== pool()?.chain_id) return { error: 'Network mismatch: select the network used by this note.' };
    return { instance };
}

function batchInstances(notes) {
    renderWithdrawalCount();
    const invalid = withdrawalInputs().findIndex((input) => validateWithdrawalNote(input.value).error);
    if (invalid !== -1) {
        const input = withdrawalInputs()[invalid];
        input.focus();
        throw new Error(`Note ${invalid + 1}: ${validateWithdrawalNote(input.value).error}`);
    }
    if (!notes.length) throw new Error('Enter at least one note.');
    return notes.map((note) => validateWithdrawalNote(note).instance);
}

function renderWithdrawalInputs(values) {
    const batch = isBatch('withdraw');
    const first = $('withdraw-note').closest('.withdraw-note-row');
    $('withdraw-notes').replaceChildren(...values.map((value, index) => {
        const row = index ? first.cloneNode(true) : first;
        const input = row.querySelector('textarea');
        input.id = index ? `withdraw-note-${index + 1}` : 'withdraw-note';
        input.value = value;
        input.required = !batch;
        input.placeholder = batch ? 'Enter a note, or paste several separated by commas or new lines' : 'Please enter your note';
        const label = row.querySelector('label');
        label.id = index ? '' : 'withdraw-note-label';
        label.htmlFor = input.id;
        label.textContent = batch ? `Note ${index + 1}` : 'Note';
        const error = row.querySelector('.note-error');
        error.id = `${input.id}-error`;
        input.setAttribute('aria-describedby', error.id);
        const remove = row.querySelector('button');
        remove.hidden = !batch;
        remove.disabled = values.length === 1;
        remove.setAttribute('aria-label', `Remove note ${index + 1}`);
        remove.onclick = () => {
            const values = withdrawalInputs().map((input) => input.value);
            values.splice(index, 1);
            renderWithdrawalInputs(values);
            withdrawalInputChanged();
        };
        return row;
    }));
    $('add-withdraw-note').hidden = !batch;
    $('withdraw-privacy-warning').hidden = !batch;
    renderWithdrawalCount();
}

function withdrawalInputChanged() {
    $('note-status').textContent = '';
    clearWithdrawalResults();
    if (!busy) message('');
}

async function readBalance(wallet, instance, address) {
    let raw;
    if (instance.native) {
        raw = await wallet.request({ method: 'eth_getBalance', params: [address, 'latest'] });
    } else {
        // ERC20Tornado.token() gives the token address for this pool.
        const token = await wallet.request({ method: 'eth_call', params: [{ to: instance.address, data: '0xfc0c546a' }, 'latest'] });
        if (!/^0x0{24}[0-9a-f]{40}$/i.test(token)) throw new Error('Invalid token address.');
        const data = `0x70a08231${address.slice(2).toLowerCase().padStart(64, '0')}`; // balanceOf(address)
        raw = await wallet.request({ method: 'eth_call', params: [{ to: `0x${token.slice(-40)}`, data }, 'latest'] });
    }
    const decimals = instance.decimals;
    const digits = BigInt(raw).toString().padStart(decimals + 1, '0');
    if (!decimals) return digits;
    return `${digits.slice(0, -decimals)}.${digits.slice(-decimals)}`.replace(/\.?0+$/, '');
}

async function updateDepositBalance() {
    const output = $('deposit-balance');
    const ids = isBatch('deposit') ? depositGroups.map((group) => group.instance) : [selected];
    const tokens = new Map();
    for (const id of ids) {
        const instance = snapshot.instances.find((item) => item.id === id);
        if (instance) tokens.set(instance.symbol, instance);
    }
    const address = account;
    const chain = pool()?.chain_id;
    const matchingNetwork = Number(walletChain) === chain;
    const key = JSON.stringify([address.toLowerCase(), chain, walletChain, [...tokens.keys()]]);
    if (balanceView?.key !== key || balanceView.provider !== provider) {
        balanceView = { key, provider, refreshAt: 0 };
        output.textContent = !address ? 'Connect wallet' : !tokens.size ? '—' : !walletChain ? 'Loading…'
            : matchingNetwork ? 'Loading…' : 'Switch network in wallet';
    }
    const view = balanceView;
    if (!provider || !address || !tokens.size || !matchingNetwork || busy || $('deposit-panel').hidden || Date.now() < view.refreshAt) return;
    view.refreshAt = Infinity;
    const balances = await Promise.all([...tokens.values()].map((instance) => readBalance(view.provider, instance, address).catch(() => null)));
    if (view !== balanceView) return;
    output.textContent = [...tokens.keys()].map((symbol, i) => balances[i] === null
        ? `${symbol.toUpperCase()} unavailable` : `${balances[i]} ${symbol.toUpperCase()}`).join(' · ');
    view.refreshAt = Date.now() + (balances.includes(null) ? 5000 : 30000);
}

function updateDepositTotal() {
    const totals = new Map();
    let count = 0;
    for (const group of depositGroups) {
        const item = snapshot.instances.find((item) => item.id === group.instance);
        if (!item || !Number.isInteger(group.count) || group.count < 1) continue;
        count += group.count;
        totals.set(item.symbol, (totals.get(item.symbol) || 0) + Number(item.unit) * group.count);
    }
    $('deposit-total').textContent = formatTotals(totals);
    $('deposit-count').textContent = `${plural(count, 'note')} · 1 deposit transaction`;
    $('start-deposit').textContent = isBatch('deposit') ? `Review ${plural(count, 'deposit')}` : 'Deposit';
    if (!$('deposit-panel').hidden && isBatch('deposit')) renderStatistics();
}

function renderDepositGroups() {
    const instances = snapshot.instances.filter((item) => item.chain_id === pool()?.chain_id);
    if (!instances.length) return;
    if (!depositGroups.length) depositGroups.push({ instance: selected, count: 2 });
    $('deposit-groups').replaceChildren(...depositGroups.map((group, index) => {
        const item = instances.find((item) => item.id === group.instance) || instances[0];
        group.instance = item.id;
        const row = element('div', null, 'deposit-group');
        const token = document.createElement('select');
        token.setAttribute('aria-label', `Token for deposit group ${index + 1}`);
        options(token, [...new Set(instances.map((item) => item.symbol))].map((symbol) => [symbol, symbol.toUpperCase()]), item.symbol);
        token.onchange = () => { group.instance = instances.find((item) => item.symbol === token.value).id; renderDepositGroups(); };
        const amount = document.createElement('select');
        amount.setAttribute('aria-label', `Pool amount for deposit group ${index + 1}`);
        options(amount, instances.filter((value) => value.symbol === item.symbol).sort((a, b) => Number(a.unit) - Number(b.unit)).map((value) => [value.id, value.unit]), item.id);
        amount.onchange = () => { group.instance = amount.value; updateDepositTotal(); };
        const quantity = document.createElement('input');
        Object.assign(quantity, { type: 'number', min: '1', step: '1', required: true, value: group.count });
        quantity.setAttribute('aria-label', `Quantity for deposit group ${index + 1}`);
        quantity.oninput = () => { group.count = Number(quantity.value); updateDepositTotal(); };
        for (const [title, control] of [['Token', token], ['Pool amount', amount], ['Qty', quantity]]) {
            const label = element('label', title);
            label.append(control);
            row.append(label);
        }
        const remove = element('button', '×');
        remove.type = 'button';
        remove.disabled = depositGroups.length === 1;
        remove.setAttribute('aria-label', `Remove deposit group ${index + 1}`);
        remove.onclick = () => { depositGroups.splice(index, 1); renderDepositGroups(); };
        row.append(remove);
        return row;
    }));
    updateDepositTotal();
    updateDepositBalance();
}

function renderWithdrawalCount() {
    const batch = isBatch('withdraw');
    const notes = withdrawalNotes();
    const count = notes.length;
    const results = withdrawalInputs().map((input) => {
        const result = batch ? validateWithdrawalNote(input.value) : {};
        input.setAttribute('aria-invalid', !!result.error);
        const error = input.closest('.withdraw-note-row').querySelector('.note-error');
        error.textContent = result.error || '';
        error.hidden = !result.error;
        return result;
    });
    const error = results.find((result) => result.error)?.error;
    const totals = new Map();
    if (!error) {
        for (const { instance } of results) if (instance) totals.set(instance.symbol, (totals.get(instance.symbol) || 0) + Number(instance.unit));
    }
    const relayer = document.querySelector('input[name=mode]:checked').value === 'relayer';
    const transactions = !relayer ? '1 wallet transaction'
        : count === 1 ? '1 relayer transaction' : `${count} separate relayer transactions`;
    $('withdraw-summary').hidden = !batch;
    $('withdraw-total').textContent = error || formatTotals(totals);
    $('withdraw-total').dataset.error = !!error;
    $('withdraw-count').hidden = !batch;
    $('withdraw-count').textContent = `${plural(count, 'note')} · ${transactions}`;
    $('check-note').textContent = batch && count !== 1 ? 'Check notes' : 'Check note';
    $('confirm-withdraw').textContent = batch ? `Withdraw ${plural(count, 'note')}` : 'Withdraw';
    if (!$('withdraw-panel').hidden && batch) renderStatistics();
}

function renderWithdrawalResults() {
    const labels = { queued: 'Queued', preparing: 'Preparing', submitted: 'Submitted', confirmed: 'Confirmed', failed: 'Failed', reverted: 'Reverted' };
    $('withdraw-results').hidden = !withdrawalResults.length;
    $('withdraw-results').replaceChildren(...withdrawalResults.map((item, index) => {
        const row = element('li');
        row.dataset.status = item.status;
        const title = element('div', null, 'batch-result-title');
        title.append(element('span', `${index + 1}. ${item.instance.unit} ${item.instance.symbol.toUpperCase()}`), element('span', labels[item.status]));
        row.append(title);
        if (item.hash) row.append(element('small', item.hash));
        if (item.error) row.append(element('small', item.error));
        return row;
    }));
}

function clearWithdrawalResults() {
    withdrawalResults = [];
    renderWithdrawalResults();
    renderWithdrawalCount();
}

function batchApprovalTokens() {
    return [...new Set((draft?.batch ? draft.notes : []).map((item) => snapshot.instances.find((value) => value.id === item.instance))
        .filter((item) => item && !item.native).map((item) => item.symbol))];
}

function nextBatchApproval() {
    return batchApprovalTokens().find((symbol) => !draft.approved.has(symbol));
}

function batchDepositParams(address) {
    const deployment = draft.deployment;
    if (batchApprovalTokens().length && !deployment?.address) throw new Error('Deploy the batch contract first.');
    if (deployment && (deployment.account.toLowerCase() !== address.toLowerCase() || deployment.chainId !== draft.instance.chain_id)) {
        throw new Error(`Switch back to ${deployment.account} on ${draft.instance.chain} to use this batch contract.`);
    }
    return { notes: depositNotes(), account: address, contract_address: deployment?.address };
}

async function deployBatch() {
    try {
        let receipt;
        if (draft.deployment?.hash) {
            receipt = await waitReceipt(draft.instance, draft.deployment.hash, draft.deployment.transaction);
        } else {
            const address = await wallet(draft.instance);
            message('Building batch contract deployment…');
            const transaction = await rpc('deploy_batch', { notes: depositNotes(), account: address });
            draft.deployment = { account: address, chainId: draft.instance.chain_id, transaction, status: 'pending' };
            receipt = await sendTransaction(transaction, draft.instance, (hash, expected) => {
                draft.deployment.hash = hash;
                draft.deployment.transaction = expected;
            }, true);
        }
        if (!/^0x[0-9a-f]{40}$/i.test(receipt.contract_address || '')) throw new ReceiptError('mismatch', 'The deployment receipt has no contract address. Check the transaction in your wallet.');
        draft.deployment.address = receipt.contract_address;
        draft.deployment.status = 'ready';
        draft.deployment.recoverable = false;
        message('Batch contract deployed. Approve the tokens to continue.');
    } catch (error) {
        if (error instanceof WalletSubmissionError) {
            draft.deployment.status = 'unknown';
            draft.deployment.recoverable = true;
        } else if (error instanceof ReceiptError && error.status === 'mismatch') draft.deployment.status = 'inspect';
        else if (!draft.deployment?.hash || error instanceof ReceiptError && error.status === 'failed') draft.deployment = null;
        throw error;
    } finally { renderBatchApprovals(); }
}

async function approveBatch(symbol) {
    if (draft.deployment?.status !== 'ready' || symbol !== nextBatchApproval()) return;
    try {
        const submitted = draft.approvalHashes.get(symbol);
        if (submitted) {
            await waitReceipt(draft.instance, submitted.hash, submitted.transaction);
        } else {
            const address = await wallet(draft.instance);
            const { approved, transaction } = await rpc('approve_batch', { ...batchDepositParams(address), symbol });
            if (!approved) await sendTransaction(transaction, draft.instance, (hash, expected) => {
                draft.approvalHashes.set(symbol, { hash, transaction: expected });
                renderBatchApprovals();
            }, true);
        }
        draft.approved.add(symbol);
        message(`${symbol.toUpperCase()} approved for this batch.`);
    } catch (error) {
        if (error instanceof ReceiptError && error.status === 'failed') draft.approvalHashes.delete(symbol);
        if (error instanceof ReceiptError && error.status === 'mismatch') {
            draft.deployment.status = 'inspect';
            draft.deployment.inspectHash = draft.approvalHashes.get(symbol)?.hash;
        }
        throw error;
    } finally { renderBatchApprovals(); }
}

function renderBatchApprovals() {
    const deployment = draft?.deployment;
    const needed = batchApprovalTokens().length > 0;
    const details = $('batch-contract-details');
    details.hidden = !needed;
    details.dataset.error = ['inspect', 'unknown'].includes(deployment?.status);
    details.textContent = !deployment ? 'Deploy a contract for this batch, then approve the tokens.'
        : deployment.status === 'unknown' ? 'The wallet did not return a deployment hash. Check your wallet activity and enter the transaction hash below.'
        : deployment.status === 'inspect' ? `Check the submitted transaction in your wallet: ${deployment.inspectHash || draft.hash || deployment.hash || ''}`
        : deployment.address ? `Batch contract: ${deployment.address}` : `Deployment submitted: ${deployment.hash}`;
    $('deploy-batch-contract').hidden = !needed;
    $('deploy-batch-contract').textContent = deployment?.address ? 'Batch contract deployed ✓'
        : deployment?.hash ? 'Check deployment' : 'Deploy batch contract';
    $('deployment-recovery').hidden = !deployment?.recoverable;
    $('batch-approvals').hidden = !needed;
    $('batch-approvals').replaceChildren(...batchApprovalTokens().map((symbol) => {
        const row = element('div', null, 'batch-approval');
        const token = symbol.toUpperCase();
        const label = draft.approved.has(symbol) ? 'Approved ✓' : draft.approvalHashes.has(symbol) ? `Check ${token} approval` : `Approve ${token}`;
        const button = element('button', label, 'outline');
        button.type = 'button';
        button.dataset.symbol = symbol;
        button.onclick = () => run(() => approveBatch(symbol));
        row.append(element('span', token), button);
        return row;
    }));
}

function renderPools() {
    const current = pool();
    const chains = [...new Map(snapshot.instances.map((item) => [item.chain_id, item.chain])).entries()];
    options($('network'), chains.map(([id, name]) => [id, name.charAt(0).toUpperCase() + name.slice(1)]), String(current?.chain_id || ''));
    const tokens = [...new Set(snapshot.instances.filter((item) => item.chain_id === current?.chain_id).map((item) => item.symbol))];
    options($('token'), tokens.map((symbol) => [symbol, symbol.toUpperCase()]), current?.symbol || '');
    const amounts = snapshot.instances.filter((item) => item.chain_id === current?.chain_id && item.symbol === current?.symbol).sort((a, b) => Number(a.unit) - Number(b.unit));
    $('amounts').replaceChildren(...amounts.map((item) => {
        const label = element('label', null, 'amount-choice');
        const radio = document.createElement('input');
        Object.assign(radio, { type: 'radio', name: 'amount', value: item.id, checked: item.id === selected });
        radio.onchange = () => { selected = item.id; renderStatus(); };
        label.append(radio, element('span', `${item.unit} ${item.symbol.toUpperCase()}`));
        return label;
    }));
    renderStatistics();
    if (isBatch('deposit')) renderDepositGroups();
    renderWithdrawalCount();
}

function renderStatistics() {
    let current = pool();
    if (!$('deposit-panel').hidden && isBatch('deposit')) {
        current = snapshot.instances.find((item) => item.id === depositGroups[0]?.instance) || current;
    } else if (!$('withdraw-panel').hidden && isBatch('withdraw')) {
        current = withdrawalInputs().map((input) => validateWithdrawalNote(input.value).instance).find(Boolean) || current;
    }
    $('selected-pool').textContent = current ? `${current.unit} ${current.symbol.toUpperCase()}` : '—';
    $('anonymity').textContent = current?.anonymity_set == null ? '—' : `${number(current.anonymity_set)} equal user deposits`;
    for (const [id, key] of [['deposits', 'deposits_total'], ['withdrawals', 'withdrawals_total'], ['synced-block', 'synced_block'], ['latest-block', 'latest_block']]) $(id).textContent = number(current?.[key]);
    const updated = current?.last_update_sec == null ? null : new Date(current.last_update_sec * 1000);
    $('last-update').textContent = updated ? updated.toLocaleString(undefined, { hourCycle: 'h23' }) : '—';
    if (!hasProgress(current)) {
        $('latest-block').textContent = '—';
        $('synced-block').textContent = current?.synced_block == null ? '—' : `${number(current.synced_block)} (cached)`;
    }
    $('pool-state').textContent = socket?.readyState === WebSocket.OPEN
        ? current?.message || current?.activity || stateLabel(current?.state)
        : 'Backend disconnected. Waiting to reconnect.';
    $('pool-address').textContent = current?.address || 'Waiting for backend';
}

function renderStatus() {
    const connected = socket?.readyState === WebSocket.OPEN;
    const instances = snapshot.instances;
    const readyCount = connected ? instances.filter((item) => item.ready).length : '—';
    $('ready-count').textContent = `${readyCount} / ${instances.length} ready`;
    $('instances').replaceChildren(...instances.map((item) => {
        const row = document.createElement('tr');
        const name = element('td', `${item.unit} ${item.symbol.toUpperCase()}`);
        name.append(element('small', item.chain, 'muted chain'));
        name.title = item.address;
        if (item.error) name.append(element('small', item.error, 'instance-error'));
        const state = element('td', connected ? stateLabel(item.state) : 'Disconnected', 'state');
        state.dataset.state = connected ? item.state : 'error';
        if (connected && item.message) state.append(element('small', item.message, 'instance-message'));
        if (connected && item.activity && item.activity !== item.message) state.append(element('small', item.activity, 'instance-message'));
        if (connected && item.sync_activity && item.sync_activity !== item.activity) state.append(element('small', item.sync_activity, 'instance-message'));
        const sync = element('td', null, 'sync');
        if (hasProgress(item)) {
            sync.textContent = `${number(item.synced_block)} / ${number(item.latest_block)}`;
            const progress = document.createElement('progress');
            progress.max = 1;
            progress.value = Math.max(0, Math.min(1, item.progress));
            progress.setAttribute('aria-label', `${item.id} block synchronization`);
            const percent = Math.min(item.remaining_blocks > 0 ? 99.9 : 100, 100 * item.progress);
            sync.append(progress, element('small', `${percent.toFixed(1)}%${item.remaining_blocks == null ? '' : ` · ${number(item.remaining_blocks)} blocks left`}`));
        } else {
            sync.textContent = item.synced_block == null ? 'Cached block —' : `Cached block ${number(item.synced_block)}`;
            sync.append(element('small', !connected ? 'Disconnected; waiting for backend'
                : item.latest_block == null ? 'Chain head not confirmed.' : `Last known head ${number(item.latest_block)}`));
        }
        const deposits = element('td', number(item.deposits_total));
        const withdrawals = element('td', number(item.withdrawals_total));
        deposits.title = `${number(item.deposits)} synced this session`;
        withdrawals.title = `${number(item.withdrawals)} synced this session`;
        row.append(name, state, sync, deposits, withdrawals);
        return row;
    }));
    if (!instances.length) {
        const row = document.createElement('tr');
        const cell = element('td', 'Waiting for backend…', 'muted');
        cell.colSpan = 5;
        row.append(cell);
        $('instances').append(row);
    }
    const rate = snapshot.relayer_fee_rate;
    $('relayer-fee').textContent = `Service fee: ${rate == null ? 'backend configured rate' : `${rate}%`} + network gas, deducted from the deposit.`;
    renderWithdrawalCount();
    renderStatistics();
    updateControls();
}

function updateControls() {
    $('deposit-fields').disabled = busy || !!draft;
    $('withdraw-fields').disabled = busy;
    $('start-deposit').disabled = busy || !!draft;
    $('check-note').disabled = busy;
    $('confirm-withdraw').disabled = busy;
    $('network').disabled = busy || !!draft || !pool();
    $('wallet').disabled = busy;
    $('wallet').textContent = account ? `${account.slice(0, 6)}…${account.slice(-4)}` : 'Connect wallet';
    $('wallet').title = account ? `${account} · wallet chain ${Number(walletChain) || 'unknown'}` : 'Connect an Ethereum wallet';
    $('connect-browser').disabled = busy;
    $('browser-status').textContent = window.ethereum ? 'Detected' : 'Not detected';
    $('browser-status').dataset.detected = !!window.ethereum;
    $('connect-walletconnect').disabled = busy;
    $('switch-wallet-account').disabled = busy;
    $('switch-wallet-account').hidden = !provider || provider !== window.ethereum;
    $('disconnect-wallet').disabled = busy;
    $('disconnect-wallet').hidden = !provider;
    $('wallet-session').hidden = !account;
    $('wallet-account').textContent = account ? `${account.slice(0, 6)}…${account.slice(-4)}` : '';
    const approval = nextBatchApproval();
    const deployment = draft?.deployment;
    const batchBlocked = !!batchApprovalTokens().length && deployment?.status !== 'ready';
    $('confirm-deposit').disabled = busy || batchBlocked || !!approval || !!draft?.approvalHash;
    $('confirm-deposit').textContent = draft?.hash ? 'Check transaction' : 'Continue';
    $('approve-token').disabled = busy || !!draft?.hash;
    $('approve-token').textContent = draft?.approvalHash ? 'Check token approval' : 'Approve token';
    for (const button of $('batch-approvals').querySelectorAll('button')) button.disabled = busy || batchBlocked || button.dataset.symbol !== approval;
    $('deploy-batch-contract').disabled = busy || !!deployment?.address || ['inspect', 'unknown'].includes(deployment?.status);
    $('check-deployment').disabled = busy;
    $('deployment-hash').disabled = busy;
    $('cancel-deposit').disabled = busy;
    $('submit-deposit').disabled = busy;
    $('back-to-note').disabled = busy;
    updateDepositBalance();
}

async function run(action) {
    if (busy) return;
    busy = true;
    message('');
    updateControls();
    try { await action(); }
    catch (error) { message(error.message || String(error), !(error instanceof TransactionWaitCancelled)); }
    finally { busy = false; progressPrefix = ''; updateControls(); }
}

async function walletRequest(walletProvider, method, params, failure) {
    try {
        return await walletProvider.request({ method, params });
    } catch (error) {
        if ([4001, 5000, 'ACTION_REJECTED'].includes(error?.code)) throw new Error('Request rejected in wallet.');
        throw new Error(failure);
    }
}

async function wallet(instance) {
    if (!provider) {
        if (window.ethereum) bindWallet(window.ethereum);
        else {
            checkBackend(instance?.id || selected);
            message('Connecting through WalletConnect…');
            bindWallet(await connectWalletConnect(snapshot.walletconnect_project_id, walletChains()));
        }
    }
    const currentProvider = provider;
    message('Requesting wallet account access. Confirm in your wallet if prompted…');
    const accounts = await walletRequest(currentProvider, 'eth_requestAccounts', [], 'Could not connect to wallet.');
    if (!accounts[0]) throw new Error('No wallet account selected.');
    if (instance) {
        const chainId = `0x${instance.chain_id.toString(16)}`;
        message('Checking wallet network…');
        const current = await walletRequest(currentProvider, 'eth_chainId', [], 'Could not read wallet network.');
        if (current.toLowerCase() !== chainId) {
            message(`Confirm the switch to ${instance.chain} in your wallet…`);
            await walletRequest(currentProvider, 'wallet_switchEthereumChain', [{ chainId }], 'Could not switch wallet network.');
        }
    }
    const address = (await walletRequest(currentProvider, 'eth_accounts', [], 'Could not read wallet account.'))[0] || '';
    if (provider !== currentProvider) throw new Error('Wallet connection changed. Please retry.');
    if (!address) throw new Error('No wallet account selected.');
    account = address;
    message('');
    updateControls();
    return account;
}

async function connectBrowserWallet() {
    if (!window.ethereum) throw new Error('No browser wallet found. Use WalletConnect instead.');
    bindWallet(window.ethereum);
    await wallet(pool());
    $('wallet-dialog').close();
    message('Wallet connected.');
}

async function revokeBrowserAccounts(browser) {
    try {
        await browser.request({ method: 'wallet_revokePermissions', params: [{ eth_accounts: {} }] });
        return true;
    } catch (error) {
        if ([-32601, -32004, 4200].includes(error?.code)) return false;
        throw new Error('Could not revoke wallet permissions.');
    }
}

async function sendTransaction(transaction, instance, submitted, verify = false) {
    checkBackend(instance.id);
    const currentProvider = provider;
    if (!currentProvider) throw new Error('Wallet disconnected. Please reconnect.');
    message('Checking wallet network and account before submission…');
    const chainId = await walletRequest(currentProvider, 'eth_chainId', [], 'Could not read wallet network.');
    const accounts = await walletRequest(currentProvider, 'eth_accounts', [], 'Could not read wallet account.');
    if (provider !== currentProvider) throw new Error('Wallet connection changed. Please retry.');
    if (Number(chainId) !== instance.chain_id || Number(transaction.chainId) !== instance.chain_id) throw new Error('Wallet network changed. Select the pool network and retry.');
    if (accounts[0]?.toLowerCase() !== transaction.from?.toLowerCase()) throw new Error('Wallet account changed. Please retry the transaction.');
    checkBackend(instance.id);
    message('Confirm transaction signing and submission in your wallet…');
    const expected = verify ? { ...transaction } : undefined;
    let hash;
    try {
        hash = await currentProvider.request({ method: 'eth_sendTransaction', params: [transaction] });
        if (verify && !/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error('Invalid transaction hash.');
    } catch (error) {
        if ([4001, 5000, 'ACTION_REJECTED'].includes(error?.code)) throw new Error('Transaction rejected in wallet.');
        if (!verify) throw new Error('Could not submit transaction. Check your wallet activity before retrying.');
        throw new WalletSubmissionError('The wallet did not return a transaction hash. Check your wallet activity before retrying.');
    }
    submitted?.(hash, expected);
    return waitReceipt(instance, hash, expected);
}

class WalletSubmissionError extends Error {}

class TransactionWaitCancelled extends Error {}

class ReceiptError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}

async function receiptDelay(signal) {
    signal.throwIfAborted();
    await new Promise((resolve) => {
        const done = () => {
            clearTimeout(timer);
            signal.removeEventListener('abort', done);
            resolve();
        };
        const timer = setTimeout(done, 5000);
        signal.addEventListener('abort', done, { once: true });
    });
    signal.throwIfAborted();
}

async function waitReceipt(instance, hash, transaction) {
    const dialog = $('receipt-dialog');
    const controller = new AbortController();
    const { signal } = controller;
    const cancel = () => controller.abort(new TransactionWaitCancelled(`Stopped waiting. You can check this transaction again: ${hash}`));
    $('stop-receipt-wait').onclick = cancel;
    dialog.oncancel = (event) => { event.preventDefault(); cancel(); };
    dialog.showModal();
    message(`Checking transaction: ${hash}`);
    const started = Date.now();
    let disconnected = false;
    try {
        for (;;) {
            await receiptDelay(signal);
            if (transaction && Date.now() - started >= 120000) {
                throw new Error(`Confirmation has not been verified. Retry to check transaction: ${hash}`);
            }
            if (socket?.readyState !== WebSocket.OPEN) {
                if (!disconnected) message(`Backend disconnected. Waiting to reconnect and check transaction: ${hash}`);
                disconnected = true;
                continue;
            }
            if (disconnected) {
                message(`Backend reconnected. Checking transaction confirmation: ${hash}`);
                disconnected = false;
            }
            let receipt;
            try { receipt = await rpc('receipt', { instance: instance.id, hash, transaction }, signal); }
            catch (error) { if (!signal.aborted && socket?.readyState !== WebSocket.OPEN) continue; throw error; }
            if (receipt.status === 'confirmed' || receipt.status === 'failed') balanceView = null;
            if (receipt.status === 'failed') throw new ReceiptError('failed', `Transaction reverted: ${hash}`);
            if (receipt.status === 'mismatch') throw new ReceiptError('mismatch', `${receipt.message || 'Submitted transaction does not match this batch.'} Transaction: ${hash}`);
            if (receipt.status === 'confirmed') { message(`Transaction confirmed: ${hash}`); return receipt; }
        }
    } finally {
        $('stop-receipt-wait').onclick = dialog.oncancel = null;
        dialog.close();
    }
}

function closeDraft() {
    $('backup-confirm-dialog').close();
    draft = null;
    $('deposit-note').value = '';
    $('backup-message').textContent = '';
    $('deployment-hash').value = '';
    renderBatchApprovals();
    updateControls();
}

for (const type of ['input', 'change', 'click']) $('deposit-panel').addEventListener(type, (event) => {
    if (!busy && !draft && (type !== 'click' || event.target.closest('#add-deposit-group, .deposit-group button'))) message('');
});
$('network').onchange = () => {
    selected = snapshot.instances.find((item) => String(item.chain_id) === $('network').value)?.id || '';
    depositGroups = [];
    clearWithdrawalResults();
    renderPools(); renderStatus(); renderWithdrawalCount();
};
$('add-deposit-group').onclick = () => { depositGroups.push({ instance: selected, count: 1 }); renderDepositGroups(); };
for (const name of ['deposit', 'withdraw']) for (const mode of ['single', 'batch']) $(`${name}-${mode}`).onclick = () => {
    if (busy || name === 'deposit' && draft) return;
    if (name === 'withdraw') {
        if (isBatch(name)) withdrawalTexts.batch = withdrawalInputs().map((input) => input.value);
        else withdrawalTexts.single = $('withdraw-note').value;
    }
    for (const value of ['single', 'batch']) $(`${name}-${value}`).setAttribute('aria-pressed', value === mode);
    if (name === 'deposit') {
        $('deposit-single-fields').hidden = mode === 'batch';
        $('deposit-batch-fields').hidden = mode !== 'batch';
        $('deposit-batch-fields').disabled = mode !== 'batch';
        if (mode === 'batch') renderDepositGroups();
        else {
            $('start-deposit').textContent = 'Deposit';
            updateDepositBalance();
        }
    } else {
        renderWithdrawalInputs(mode === 'batch' ? withdrawalTexts.batch : [withdrawalTexts.single]);
        $('note-status').textContent = '';
        clearWithdrawalResults();
        renderWithdrawalCount();
    }
    renderStatistics();
};
$('clear-logs').onclick = () => { $('logs').replaceChildren(); updateLogStatus(); };
$('auto-scroll').onchange = () => { if ($('auto-scroll').checked) $('logs').scrollTop = $('logs').scrollHeight; };
$('live-logs').ontoggle = () => { if ($('live-logs').open && $('auto-scroll').checked) $('logs').scrollTop = $('logs').scrollHeight; };
$('token').onchange = () => { selected = snapshot.instances.find((item) => item.chain_id === pool()?.chain_id && item.symbol === $('token').value)?.id || ''; renderPools(); renderStatus(); };
$('wallet').onclick = () => { message(''); $('wallet-message').textContent = ''; $('wallet-dialog').showModal(); };
$('close-wallet').onclick = () => $('wallet-dialog').close();
$('connect-browser').onclick = () => run(connectBrowserWallet);
$('switch-wallet-account').onclick = () => run(async () => {
    if (!await revokeBrowserAccounts(provider)) {
        throw new Error('Change this site’s connected account in your browser wallet.');
    }
    await connectBrowserWallet();
});
$('connect-walletconnect').onclick = () => run(async () => {
    checkBackend(selected);
    bindWallet(await connectWalletConnect(snapshot.walletconnect_project_id, walletChains()));
    await wallet(pool());
    message('Wallet connected.');
});
$('disconnect-wallet').onclick = () => run(async () => {
    const previous = provider;
    const browser = previous && previous === window.ethereum;
    const revoked = !browser || await revokeBrowserAccounts(previous);
    bindWallet(null);
    if (!browser) await previous?.disconnect?.();
    $('wallet-dialog').close();
    message(revoked ? 'Wallet disconnected.' : 'Wallet disconnected here. Manage this site’s permissions in your browser wallet.');
});
$('copy-wallet-uri').onclick = async () => {
    if (!$('wallet-uri').value) { message('Waiting for the connection link…', true); return; }
    try { await navigator.clipboard.writeText($('wallet-uri').value); message('Connection link copied.'); }
    catch { $('wallet-uri').focus(); $('wallet-uri').select(); message('Select and copy the connection link manually.', true); }
};
for (const name of ['deposit', 'withdraw']) {
    $(`${name}-panel`).addEventListener('invalid', (event) => message(event.target.validationMessage, true), true);
    $(`${name}-tab`).onclick = () => {
        if (!busy) message('');
        for (const tab of ['deposit', 'withdraw']) {
            $(`${tab}-tab`).setAttribute('aria-selected', tab === name);
            $(`${tab}-panel`).hidden = tab !== name;
        }
        renderStatus();
    };
}
$('deposit-panel').onsubmit = (event) => {
    event.preventDefault();
    run(async () => {
        const batch = isBatch('deposit');
        const instance = batch ? snapshot.instances.find((item) => item.id === depositGroups[0]?.instance) : pool();
        checkBackend(selected);
        await wallet(instance);
        message(batch ? 'Creating deposit notes…' : 'Creating deposit note…');
        draft = batch ? { ...await rpc('create_notes', { entries: depositGroups }), instance, batch: true, approved: new Set(), approvalHashes: new Map() }
            : { ...await rpc('create_note', { instance: instance.id }), instance };
        const multiple = batch && draft.notes.length > 1;
        $('deposit-note').value = depositNotes().join('\n');
        $('backup-title').textContent = multiple ? `Back up all ${draft.notes.length} notes` : 'Back up your note';
        $('backup-description').textContent = multiple ? 'Each deposit has its own note. Save the complete backup before continuing.'
            : 'Your note is the only way to withdraw your deposit. Save it before continuing.';
        $('download-note').textContent = multiple ? 'Download all notes' : 'Download note';
        $('copy-note').textContent = multiple ? 'Copy all notes' : 'Copy note';
        $('backup-confirm-title').textContent = multiple ? `Have you saved all ${draft.notes.length} notes?` : 'Have you saved your note?';
        $('backup-confirm-description').replaceChildren(element('strong', multiple
            ? 'Without these notes, you cannot withdraw your deposits.' : 'Without your note, you cannot withdraw your deposit.'), document.createTextNode(' Lost notes cannot be recovered.'));
        $('submit-deposit').textContent = multiple ? 'I saved all notes — deposit' : 'I saved my note — deposit';
        $('back-to-note').textContent = multiple ? 'Back to notes' : 'Back to note';
        $('backup-message').textContent = '';
        $('approve-token').hidden = batch || instance.native;
        renderBatchApprovals();
        $('backup-dialog').showModal();
        if (nextBatchApproval()) message(multiple ? 'Save all notes, deploy the batch contract, and approve each token before continuing.'
            : 'Save your note, deploy the batch contract, and approve the token before continuing.');
        else message(multiple ? 'Save all notes, then continue to confirm the deposits.' : 'Save your note, then continue to confirm the deposit.');
    });
};
$('cancel-deposit').onclick = () => $('backup-dialog').close();
$('backup-dialog').onclose = closeDraft;
$('backup-dialog').oncancel = (event) => { if (busy) event.preventDefault(); };
$('backup-confirm-dialog').oncancel = (event) => { if (busy) event.preventDefault(); };
$('back-to-note').onclick = () => $('backup-confirm-dialog').close();
$('confirm-deposit').onclick = () => {
    if (busy) return;
    if (!draft) { message('Create a deposit note first.', true); return; }
    if (draft.hash) { run(submitDeposit); return; }
    $('backup-confirm-message').textContent = '';
    $('backup-confirm-dialog').showModal();
};
$('copy-note').onclick = async () => {
    try { await navigator.clipboard.writeText(depositNotes().join('\n')); message(depositNotes().length > 1 ? 'All notes copied.' : 'Note copied.'); }
    catch { $('deposit-note').focus(); $('deposit-note').select(); message('Select and copy the note manually.', true); }
};
$('download-note').onclick = () => {
    try {
        const url = URL.createObjectURL(new Blob([depositNotes().join('\n') + '\n'], { type: 'text/plain' }));
        const link = Object.assign(document.createElement('a'), { href: url, download: `tornado-${draft.batch ? `batch-${draft.instance.chain}` : draft.instance.id}-${Date.now()}.txt` });
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        message(depositNotes().length > 1 ? 'Download requested. Check that all notes were saved.' : 'Download requested. Check that the note file was saved.');
    } catch (error) { message(error.message || String(error), true); }
};
$('approve-token').onclick = () => run(async () => {
    checkBackend(draft.instance.id);
    try {
        if (draft.approvalHash) await waitReceipt(draft.instance, draft.approvalHash);
        else {
            const address = await wallet(draft.instance);
            message('Building token approval transaction…');
            const tx = await rpc('approve', { instance: draft.instance.id, account: address });
            await sendTransaction(tx, draft.instance, (hash) => { draft.approvalHash = hash; });
        }
    } catch (error) {
        if (error instanceof ReceiptError) draft.approvalHash = '';
        throw error;
    }
    draft.approvalHash = '';
    message('Token approved. You can now deposit.');
});
$('deploy-batch-contract').onclick = () => run(deployBatch);
$('check-deployment').onclick = () => run(async () => {
    const hash = $('deployment-hash').value.trim();
    if (!/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error('Enter the deployment transaction hash from your wallet.');
    draft.deployment.hash = hash;
    await deployBatch();
});
async function submitDeposit() {
    if (!draft || !draft.hash && !$('backup-confirm-dialog').open) throw new Error('Confirm that your note is saved before depositing.');
    if ($('backup-confirm-dialog').open) $('backup-confirm-dialog').close();
    checkBackend(draft.instance.id);
    try {
        if (draft.hash) await waitReceipt(draft.instance, draft.hash, draft.depositTx);
        else {
            const address = await wallet(draft.instance);
            message(draft.batch ? 'Building batch deposit transaction…' : 'Building deposit transaction…');
            const tx = draft.batch ? await rpc('deposit_batch', batchDepositParams(address))
                : await rpc('deposit', { instance: draft.instance.id, account: address, note: draft.note });
            await sendTransaction(tx, draft.instance, (hash, expected) => {
                draft.hash = hash;
                draft.depositTx = expected;
            }, !!draft.deployment);
        }
    } catch (error) {
        if (error instanceof ReceiptError && error.status === 'failed') draft.hash = '';
        if (draft.deployment && error instanceof ReceiptError && error.status === 'mismatch') draft.deployment.status = 'inspect';
        renderBatchApprovals();
        throw error;
    }
    $('backup-dialog').close();
}
$('submit-deposit').onclick = () => run(submitDeposit);
for (const radio of document.querySelectorAll('input[name=mode]')) radio.onchange = () => {
    const relayer = radio.value === 'relayer' && radio.checked;
    $('relayer-field').hidden = !relayer;
    $('relayer').required = relayer;
    $('accept-fee').required = relayer;
    $('accept-fee').checked = false;
    clearWithdrawalResults();
    renderWithdrawalCount();
    if (!busy) message('');
};
$('withdraw-notes').oninput = withdrawalInputChanged;
$('withdraw-notes').onpaste = (event) => {
    if (!isBatch('withdraw') || event.target.tagName !== 'TEXTAREA') return;
    const text = event.clipboardData?.getData('text');
    if (!text || !/[,\r\n]/.test(text)) return;
    event.preventDefault();
    const input = event.target;
    const values = withdrawalInputs().map((input) => input.value);
    const index = withdrawalInputs().indexOf(input);
    const pasted = `${input.value.slice(0, input.selectionStart)}${text}${input.value.slice(input.selectionEnd)}`.split(/[,\r\n]+/).map((note) => note.trim()).filter(Boolean);
    values.splice(index, 1, ...(pasted.length ? pasted : ['']));
    renderWithdrawalInputs(values);
    withdrawalInputChanged();
    withdrawalInputs()[index + Math.max(0, pasted.length - 1)].focus();
};
$('add-withdraw-note').onclick = () => {
    renderWithdrawalInputs([...withdrawalInputs().map((input) => input.value), '']);
    withdrawalInputs().at(-1).focus();
};
for (const id of ['recipient', 'relayer']) $(id).oninput = () => {
    clearWithdrawalResults();
    if (!busy) message('');
};
$('check-note').onclick = () => run(async () => {
    if (isBatch('withdraw')) {
        const notes = withdrawalNotes();
        const instances = batchInstances(notes);
        message('Checking note statuses…');
        const result = await rpc('note_status_batch', { notes });
        $('note-status').textContent = result.notes.map((item, index) => `${index + 1}. ${instances[index].unit} ${instances[index].symbol.toUpperCase()} · ${item.deposited ? 'Deposited' : 'Not deposited'} · ${item.spent ? 'Already spent' : 'Unspent'}`).join('\n');
        message('Note statuses checked.');
        return;
    }
    message('Checking note status…');
    const result = await rpc('note_status', { note: $('withdraw-note').value.trim() });
    $('note-status').textContent = `${result.deposited ? 'Deposited' : 'Not deposited'} · ${result.spent ? 'Already spent' : 'Unspent'} · ${number(result.deposits_after)} deposits and ${number(result.withdrawals_after)} withdrawals after yours.`;
    message('Note status checked.');
});
$('withdraw-panel').onsubmit = (event) => {
    event.preventDefault();
    run(async () => {
        if (isBatch('withdraw')) {
            batchInstances(withdrawalNotes());
            if (await confirmBatchWithdrawal()) await withdrawBatch();
            return;
        }
        const note = $('withdraw-note').value.trim();
        const recipient = $('recipient').value.trim();
        try {
            const previous = singleWithdrawals.get(note);
            if (previous) await waitReceipt(previous.instance, previous.hash);
            else await withdrawSingle(note, recipient);
        } catch (error) {
            if (error instanceof ReceiptError) singleWithdrawals.delete(note);
            throw error;
        }
        singleWithdrawals.delete(note);
    });
};

async function withdrawSingle(note, recipient) {
    const { instance, error } = validateWithdrawalNote(note, false);
    if (!instance) throw new Error(error || 'Enter your note.');
    checkBackend(instance.id);
    const submitted = (hash) => singleWithdrawals.set(note, { instance, hash });
    if (document.querySelector('input[name=mode]:checked').value === 'relayer') {
        const relayer = $('relayer').value.trim();
        if (!/^https?:\/\//i.test(relayer)) throw new Error('Enter an HTTP or HTTPS relayer URL.');
        message('Preparing withdrawal for the relayer…');
        const result = await rpc('relay_withdraw', { note, recipient, relayer });
        submitted(result.hash);
        await waitReceipt(instance, result.hash);
    } else {
        const address = await wallet(instance);
        message('Building withdrawal transaction…');
        const tx = await rpc('withdraw', { note, recipient, account: address });
        await sendTransaction(tx, instance, submitted);
    }
}

function confirmBatchWithdrawal() {
    const dialog = $('withdraw-confirm-dialog');
    dialog.returnValue = '';
    dialog.showModal();
    $('cancel-batch-withdraw').focus();
    return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'continue'), { once: true }));
}
$('cancel-batch-withdraw').onclick = $('close-batch-withdraw').onclick = () => $('withdraw-confirm-dialog').close();
$('submit-batch-withdraw').onclick = () => $('withdraw-confirm-dialog').close('continue');

async function withdrawBatch() {
    const notes = withdrawalNotes();
    const instances = batchInstances(notes);
    $('note-status').textContent = '';
    const recipient = $('recipient').value.trim();
    const relayed = document.querySelector('input[name=mode]:checked').value === 'relayer';
    const relayer = $('relayer').value.trim();
    if (relayed && !/^https?:\/\//i.test(relayer)) throw new Error('Enter an HTTP or HTTPS relayer URL.');
    const key = JSON.stringify([notes, recipient.toLowerCase(), instances[0].chain_id]);
    const previous = relayed ? null : walletBatches.get(key);
    if (previous?.[0].hash && previous[0].status !== 'reverted') {
        withdrawalResults = previous;
        renderWithdrawalResults();
        if (previous[0].status === 'confirmed') { message(`Transaction already confirmed: ${previous[0].hash}`); return; }
        try {
            await waitReceipt(instances[0], previous[0].hash);
            previous.forEach((item) => { item.status = 'confirmed'; item.error = ''; });
        } catch (error) {
            if (error instanceof TransactionWaitCancelled) throw error;
            previous.forEach((item) => { item.error = error.message; item.status = error.message.startsWith('Transaction reverted:') ? 'reverted' : 'submitted'; });
            throw error;
        } finally { renderWithdrawalResults(); }
        return;
    }
    withdrawalResults = notes.map((note, index) => {
        const job = relayed && relayerJobs.get(note);
        if (job && (job.recipient.toLowerCase() !== recipient.toLowerCase() || job.relayer !== relayer || job.instance.chain_id !== instances[index].chain_id)) {
            throw new Error(`Note ${index + 1} already has a relayer job with different withdrawal details${job.hash ? `: ${job.hash}` : '.'}`);
        }
        if (job) job.error = '';
        return job || { note, instance: instances[index], recipient, relayer, status: 'queued', hash: '', error: '' };
    });
    renderWithdrawalResults();
    if (!relayed) {
        const address = await wallet(instances[0]);
        withdrawalResults.forEach((item) => { item.status = 'preparing'; });
        renderWithdrawalResults();
        try {
            const tx = await rpc('withdraw_batch', { notes, account: address, recipient });
            await sendTransaction(tx, instances[0], (hash) => {
                withdrawalResults.forEach((item) => { item.hash = hash; item.status = 'submitted'; });
                walletBatches.set(key, withdrawalResults);
                renderWithdrawalResults();
            });
            withdrawalResults.forEach((item) => { item.status = 'confirmed'; });
        } catch (error) {
            if (error instanceof TransactionWaitCancelled) throw error;
            withdrawalResults.forEach((item) => {
                item.error = error.message;
                item.status = error.message.startsWith('Transaction reverted:') ? 'reverted' : item.hash ? 'submitted' : 'failed';
            });
            throw error;
        } finally { renderWithdrawalResults(); }
        return;
    }
    for (const [index, item] of withdrawalResults.entries()) {
        if (item.status === 'confirmed') continue;
        progressPrefix = `Withdrawal ${index + 1}/${notes.length}`;
        item.error = '';
        try {
            if (!item.hash || item.status === 'reverted') {
                item.hash = '';
                item.status = 'preparing';
                renderWithdrawalResults();
                message('Preparing withdrawal for the relayer…');
                const result = await rpc('relay_withdraw', { note: item.note, recipient, relayer });
                item.hash = result.hash;
                relayerJobs.set(item.note, item);
            }
            item.status = 'submitted';
            renderWithdrawalResults();
            await waitReceipt(item.instance, item.hash);
            item.status = 'confirmed';
        } catch (error) {
            if (error instanceof TransactionWaitCancelled) throw error;
            item.error = error.message || String(error);
            item.status = item.error.startsWith('Transaction reverted:') ? 'reverted' : item.hash ? 'submitted' : 'failed';
        }
        renderWithdrawalResults();
        if (socket?.readyState !== WebSocket.OPEN) break;
    }
    progressPrefix = '';
    const remaining = withdrawalResults.filter((item) => item.status !== 'confirmed').length;
    message(remaining ? `${notes.length - remaining}/${plural(notes.length, 'withdrawal')} confirmed. ${remaining} remaining; retry to continue.`
        : notes.length === 1 ? '1 withdrawal confirmed.' : `${notes.length} withdrawals confirmed in separate transactions.`, !!remaining);
}

function walletChains() {
    return [...new Set(snapshot.instances.map((item) => item.chain_id))];
}

function bindWallet(next) {
    provider = next;
    account = walletChain = '';
    if (!next) { updateControls(); return; }
    if (!providers.has(next)) {
        providers.add(next);
        next.on?.('accountsChanged', (accounts) => { if (provider === next) { account = accounts[0] || ''; updateControls(); } });
        next.on?.('chainChanged', (chainId) => { if (provider === next) { walletChain = chainId; updateControls(); } });
        next.on?.('disconnect', () => { if (provider === next) { bindWallet(null); message('Wallet disconnected. Please reconnect.', true); } });
    }
    next.request({ method: 'eth_accounts' }).then((accounts) => { if (provider === next) { account = accounts[0] || ''; updateControls(); } }).catch(() => {});
    next.request({ method: 'eth_chainId' }).then((chainId) => { if (provider === next) { walletChain = chainId; updateControls(); } }).catch(() => {});
}
window.addEventListener('ethereum#initialized', () => { if (!provider) bindWallet(window.ethereum); });
bindWallet(window.ethereum);
updateControls();
connect();
