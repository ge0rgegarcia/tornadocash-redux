from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from typing import Callable
from web3 import Web3
from web3.contract import Contract
from web3.types import Nonce, TxParams, Wei
import threading

from . import relayer, wallet
from blockchain import database, rpc
from blockchain.sync import ChainReader, EventDeposit, EventWithdraw, PoolPoller
from util import helper_func, log
from util.helper_func import get_symbol_type, unit_to_wei, wait
from util.mytype import ChainID, CircuitInput, Key, MerkleProof, Metadata, Note, Second, Symbol, SymbolType, TornadoUnit
from zk import merkle_tree
import config
import zk


class Tornado(PoolPoller.Handler):

    class Handler(object):

        def on_first_catchup(self, chain: ChainID, symbol: Symbol, unit: TornadoUnit) -> None:
            """Called when the first catchup is done."""
            pass

        def on_sync(self, block_from: int, block_to: int, deposits: list[EventDeposit], withdrawals: list[EventWithdraw]) -> None:
            """Called when a sync is done."""
            pass

        def on_latest_block(self, block_number: int) -> None:
            """Called when the latest block number is updated."""
            pass

        def on_progress(self, message: str) -> None:
            """Called before an operation starts its next step."""
            pass

    def __init__(self, chain: ChainID, symbol: Symbol, unit: TornadoUnit, connection: rpc.Interface | None = None,
                 *, reader: ChainReader | None = None) -> None:
        self.tag                : str                          = f'{__class__.__name__}'
        self.chain              : ChainID                      = chain
        self.symbol             : Symbol                       = symbol
        self.unit               : TornadoUnit                  = unit
        self.connection         : rpc.Interface | None         = connection
        self.external           : bool                         = connection is not None  # If True, the RPC connection is managed outside
        self.initialized        : bool                         = False
        self.sync_only          : bool                         = False  # If True, only sync events; withdrawal is unavailable
        self.db                 : database.Interface           = database.create(database.Backend.SQLITE)
        self.meta               : Metadata                     = helper_func.load_metadata()[chain]
        self.proxy_address      : ChecksumAddress              = self.meta.proxy_address
        self.deployment_address : ChecksumAddress              = self.meta.deployment[self.symbol][self.unit][0]
        self.proxy_contract     : Contract | rpc.Error | None  = None
        self.deployment_contract: Contract | rpc.Error | None  = None
        self.token_contract     : Contract | rpc.Error | None  = None
        self.catchup            : bool                         = False
        self.last_update_sec    : Second | None                = None
        self.tree               : merkle_tree.Interface | None = None
        self.zksnark            : zk.circuit.Interface         = zk.circuit.create(zk.circuit.ImplType(config.ZK_BACKEND))
        self.mutex              : threading.Lock               = threading.Lock()
        self.handlers           : list[Tornado.Handler]        = []
        with open(config.TORNADO_PROXY_ABI_PATH, 'r') as f:
            proxy_abi: str = f.read()
        with open(config.TORNADO_ABI_PATH, 'r') as f:
            deployment_abi: str = f.read()
        with open(config.ERC20_ABI_PATH, 'r') as f:
            erc20_abi: str = f.read()
        if self.connection is None:
            self.connection = rpc.Connection(self.chain, config.RPC_URLS[self.chain])
            if not self.connection.start():
                raise RuntimeError(f'Failed to start RPC connection for {self.chain}')
        if reader is None:
            reader = ChainReader(self.connection, expected_chain_id=self.chain.value)
        self.poller             : PoolPoller                   = PoolPoller(self.chain, self.connection, reader=reader)
        self.proxy_contract      = self.connection.get_contract(address=self.proxy_address, abi=proxy_abi)
        self.deployment_contract = self.connection.get_contract(self.deployment_address, deployment_abi)
        self.token_contract      = self.connection.get_contract(self.meta.token_address[self.symbol], erc20_abi) if SymbolType.ERC20 == get_symbol_type(self.symbol) else None
        if rpc.is_error(self.proxy_contract):
            raise RuntimeError(f'Failed to get proxy contract instance, RPC error: {self.proxy_contract.code.value}')
        if rpc.is_error(self.deployment_contract):
            raise RuntimeError(f'Failed to get deployment contract instance, RPC error: {self.deployment_contract.code.value}')
        if rpc.is_error(self.token_contract):
            raise RuntimeError(f'Failed to get token contract instance, RPC error: {self.deployment_contract.code.value}')

    def __del__(self):
        if not self.external:
            if self.connection.started():
                self.connection.stop()

    '''
    Initialize Tornado instance
    @param  sync_only   If True, only sync events without rebuilding the merkle tree, 'withdraw()' will not be available
    '''
    def init(self, sync_only: bool) -> bool:
        if self.initialized:
            if self.sync_only and not sync_only:
                return self._init_tree()
            log.debug(self.tag, 'init() already initialized')
            return True
        # Start RPC connection
        if not self.external:
            if not self.connection.start():
                log.error(self.tag, f'Failed to start RPC connection for {self.chain}')
                return False
        elif not self.connection.started():
            log.error(self.tag, 'The external RPC connection is not started')
            return False
        # Open database
        if not self.db.open(self.chain, self.symbol, self.unit):
            log.error(self.tag, f'Opening database failed for {self.chain}_{self.unit}{self.symbol}')
            return False
        # Check integrity
        if not self.db.check_integrity():
            log.error(self.tag, 'init() failed to check database integrity')
            self.db.close()
            return False
        # Rebuild merkle tree
        self.sync_only = True
        if not sync_only and not self._init_tree():
            self.db.close()
            return False
        # Add handlers
        self.poller.add_handler(self)
        self.initialized = True
        log.debug(self.tag, f'init()')
        return True

    def _init_tree(self) -> bool:
        with self.mutex:
            if self.tree is None:
                log.info(self.tag, 'Rebuilding merkle tree from database, please wait...')
                self._progress('Reading commitments from database')
                commitments: list[HexBytes] | None = self.db.get_commitments()
                if commitments is None:
                    log.error(self.tag, 'init() failed to get commitments from database')
                    return False
                try:
                    self._progress('Preparing Merkle tree')
                    self.tree = merkle_tree.create(
                        merkle_tree.ImplType.MEMORY, config.MERKLE_TREE_HEIGHT, commitments,
                        lambda level, height, completed, total: self._progress(
                            f'Rebuilding Merkle tree: level {level}/{height} · {completed:,}/{total:,} nodes')
                    )
                except ValueError:
                    log.error(self.tag, 'init() failed to rebuild the merkle tree')
                    return False
                log.info(self.tag, 'Merkle tree ready')
                self._progress('Merkle tree ready')
            self.sync_only = False
        return True

    def un_init(self) -> None:
        if not self.initialized:
            log.debug(self.tag, 'un_init() already un-initialized')
            return
        log.debug(self.tag, 'un_init() shutting down')
        self.initialized = False
        if self.poller.is_started():
            self.poller.stop()
        self.poller.remove_all_handlers()
        self.db.close()
        if not self.external:
            if self.connection.started():
                self.connection.stop()
        self.tree = None
        self.sync_only = False
        log.debug(self.tag, 'un_init() done')

    def is_initialized(self) -> bool:
        return not self.initialized

    def is_catchup(self) -> bool:
        with self.mutex:
            return self.catchup

    def add_handler(self, handler: Handler) -> None:
        with self.mutex:
            self.handlers.append(handler)

    def remove_all_handlers(self) -> None:
        with self.mutex:
            self.handlers.clear()

    def _progress(self, message: str) -> None:
        for h in self.handlers[:]:
            h.on_progress(message)

    # Check if a note has been deposited
    # @param    commitment      The commitment to query
    # @return   True : The corresponding note has been deposited
    #           False: The corresponding note has not been used
    #           None : Failed to query the blockchain, e.g. RPC connection error
    def note_deposited(self, commitment: HexBytes) -> bool | None:
        result: bool | rpc.Error = rpc.contract_call(self.deployment_contract, 'commitments', commitment)
        if rpc.is_error(result):
            log.error(self.tag, f'note_deposited(), failed to call contract function commitments(bytes32), RPC error: {result.code.value}')
            return None
        return result

    # Check if a note has been withdrawn
    # @param    nullifier_hash  The nullifier hash to query
    # @return   True : The corresponding note has been withdrawn
    #           False: The corresponding note has not been used
    #           None : Failed to query the blockchain, e.g. RPC connection error
    def note_withdrawn(self, nullifier_hash: HexBytes) -> bool | None:
        result: bool | rpc.Error = rpc.contract_call(self.deployment_contract, 'nullifierHashes', nullifier_hash)
        if rpc.is_error(result):
            log.error(self.tag, f'note_withdrawn(), failed to call contract function nullifierHashes(bytes32), RPC error: {result.code.value}')
            return None
        return result

    # Get how many deposit and withdrawal events happened after the note was deposited
    # @param    commitment  The commitment of the note to query
    # @return   Tuple of (num_deposits, num_withdrawals) on success, None if failed
    def note_age(self, commitment: HexBytes) -> tuple[int, int] | None:
        return self.db.note_age(commitment)

    # Create unsigned approve transaction of 'self.unit' amounts of tokens for deposit
    # @param    address     The address to make the approval, must use the corresponding private key to sign the transaction
    # @param    nonce       The nonce of the address, if None, will be queried from the blockchain
    # @return   TxParams of the unsigned transaction on success, rpc.Error or None if failed
    def approve_tx(self, address: ChecksumAddress, nonce: Nonce | rpc.Error | None = None) -> TxParams | rpc.Error | None:
        if nonce is None:
            self._progress('Fetching approval transaction nonce')
            nonce = self.connection.nonce(address)
            if rpc.is_error(nonce):
                log.error(self.tag, f'approve_tx(), failed to get nonce, RPC error: {nonce.code.value}')
                return None
        tx: TxParams = {
            'from'   : address,
            'chainId': self.chain.value,
            'nonce'  : Nonce(nonce),
        }
        self._progress('Building approval transaction')
        built_tx: TxParams | rpc.Error = rpc.contract_build_tx(
            self.token_contract, 'approve', tx,
            self.proxy_address, unit_to_wei(self.unit, self.meta.decimals[self.symbol])
        )
        if rpc.is_error(built_tx):
            log.error(self.tag, f'approve_tx(), failed to build transaction, RPC error: {built_tx}')
        return built_tx

    # Approve 'self.unit' amounts of tokens for deposit
    # @param    key     The private key to make the approval
    # @param    nonce   The nonce of the address, if None, will be queried from the blockchain
    # @return   HexBytes of transaction hash on success, None if failed
    def approve(self, key: Key, nonce: Nonce | rpc.Error | None = None) -> HexBytes | None:
        if nonce is None:
            nonce = self.connection.nonce(key.eth_address())
            if rpc.is_error(nonce):
                log.error(self.tag, f'approve(), failed to get nonce, RPC error: {nonce.code.value}')
                return None
        # Build
        built_tx: TxParams | rpc.Error | None = self.approve_tx(key.eth_address(), nonce)
        tx_hash = wallet.send(self.connection, built_tx, key, self.tag, 'approve()')
        if tx_hash is None:
            return None
        # Wait for the allowance to be updated
        max_waits: int = 30
        counter  : int = 0
        while True:
            counter += 1
            allowance = rpc.contract_call(self.token_contract, 'allowance', key.eth_address(), self.proxy_address)
            if rpc.is_error(allowance):
                log.error(self.tag, f'approve(), failed to call contract function allowance(address,address), RPC error: {allowance.code.value}')
                return None
            if allowance >= unit_to_wei(self.unit, self.meta.decimals[self.symbol]):
                break
            if counter >= max_waits:
                log.error(self.tag, f'approve(), timeout waiting for allowance to be updated')
                return None
            log.info(self.tag, f'approve(), waiting for allowance to be updated...')
            wait(Second(2))
        return tx_hash

    # Create unsigned deposit transaction
    # @param    commitment      The commitment of the note to deposit
    # @param    from_address    The address to make the deposit, must use the corresponding private key to sign the transaction
    # @param    nonce           The nonce of the from_address, if None, will be queried from the blockchain
    # @return   TxParams of the unsigned transaction on success, rpc.Error or None if failed
    def deposit_tx(self, commitment: HexBytes, from_address: ChecksumAddress, nonce: Nonce | rpc.Error | None = None) -> TxParams | rpc.Error | None:
        if nonce is None:
            self._progress('Fetching deposit transaction nonce')
            nonce = self.connection.nonce(from_address)
            if rpc.is_error(nonce):
                log.error(self.tag, f'deposit_tx(from={from_address}), failed to get nonce, RPC error: {nonce.code.value}')
                return None
        tx: TxParams = {
            'from'   : from_address,
            'chainId': self.chain.value,
            'nonce'  : Nonce(nonce),
        }
        if SymbolType.NATIVE == get_symbol_type(self.symbol):
            tx['value'] = unit_to_wei(self.unit, self.meta.decimals[self.symbol])
        self._progress('Building deposit transaction')
        built_tx: TxParams | rpc.Error = rpc.contract_build_tx(self.proxy_contract, 'deposit', tx, self.deployment_address, commitment, b'')
        if rpc.is_error(built_tx):
            log.error(self.tag, f'deposit_tx(from={from_address}), failed to build transaction, RPC error: {built_tx}')
        return built_tx

    # Deposit
    # @param    commitment  The commitment of the note to deposit
    # @param    key         The private key to make the deposit
    # @param    nonce       The nonce of the from_address, if None, will be queried from the blockchain
    # @return   HexBytes of transaction hash on success, None if failed
    def deposit(self, commitment: HexBytes, key: Key, nonce: Nonce | rpc.Error | None = None) -> HexBytes | None:
        if nonce is None:
            nonce = self.connection.nonce(key.eth_address())
            if rpc.is_error(nonce):
                log.error(self.tag, f'deposit(from={key.eth_address()}), failed to get nonce, RPC error: {nonce.code.value}')
                return None
        # Allowance for ERC20 token
        if SymbolType.ERC20 == get_symbol_type(self.symbol):
            # Check allowance
            allowance: int | rpc.Error = rpc.contract_call(self.token_contract, 'allowance', key.eth_address(), self.proxy_address)
            if rpc.is_error(allowance):
                log.error(self.tag, f'deposit(from={key.eth_address()}), failed to call contract function allowance(address,address), RPC error: {allowance.code.value}')
                return None
            # Approve if not enough
            if allowance < unit_to_wei(self.unit, self.meta.decimals[self.symbol]):
                tx_hash: HexBytes | None = self.approve(key, nonce)
                if tx_hash is None:
                    return None
            # Increase nonce manually
            nonce += 1
        # Build
        built_tx: TxParams | rpc.Error | None = self.deposit_tx(commitment, key.eth_address(), nonce)
        tx_hash = wallet.send(self.connection, built_tx, key, self.tag,
                              f'deposit(from={key.eth_address()})')
        if tx_hash is None:
            return None
        log.info(self.tag, f'Deposit {self.unit.value} {self.symbol.value.upper()} succeed, tx hash: {tx_hash.hex()}', log.Color.CYAN, log.Style.BOLD)
        return tx_hash

    def prepare_withdraw(self,
                         note     : Note,
                         recipient: ChecksumAddress,
                         relayer  : ChecksumAddress = ChecksumAddress('0x0000000000000000000000000000000000000000'),
                         fee      : Wei             = Wei(0),
                         refund   : Wei             = Wei(0)) -> tuple[MerkleProof, dict] | None:
        # Get merkle proof
        if self.sync_only or self.tree is None:
            log.error(self.tag, 'prepare_withdraw(), full initialization is required before withdrawal')
            return None
        self._progress('Reading Merkle path')
        with self.mutex:
            index       : int | None         = self.tree.get_index(note.commitment)
            merkle_proof: MerkleProof | None = self.tree.get_merkle_proof(index) if index is not None else None
        if merkle_proof is None:
            log.error(self.tag, 'prepare_withdraw(), deposit is missing from the local tree')
            return None
        self._progress('Generating zero-knowledge proof')
        # Get ZK proof
        zk_proof: dict | None = self.zksnark.prove(CircuitInput(
            merkle_proof=merkle_proof,
            note=note,
            recipient=recipient,
            relayer=relayer,
            fee=fee,
            refund=refund,
        ))
        if zk_proof is None:
            log.error(self.tag, 'prepare_withdraw(), failed to generate proof')
            return None
        # ZK Verify
        self._progress('Verifying zero-knowledge proof')
        if not self.zksnark.verify(zk_proof):
            log.error(self.tag, 'prepare_withdraw(), failed to verify proof')
            return None
        log.info(self.tag, f'Proof generated for commitment: {note.commitment.to_0x_hex()}')
        return merkle_proof, zk_proof

    # Create unsigned withdraw transaction
    # @param    note            The note to withdraw
    # @param    withdrawer      The address to make the withdrawal, must use the corresponding private key to sign the transaction
    # @param    recipient       Address to receive the withdrawal
    # @param    merkle_proof    The merkle proof of the note
    # @param    zk_proof        The zk-SNARK proof of the note
    # @param    nonce           The nonce of the withdrawer address, if None, will be queried from the blockchain
    # @param    refund          Amount to send to the recipient
    # @return   TxParams of the unsigned transaction on success, rpc.Error or None if failed
    def withdraw_tx(self,
                    note: Note,
                    withdrawer: ChecksumAddress,
                    recipient: ChecksumAddress,
                    merkle_proof: MerkleProof,
                    zk_proof: dict,
                    nonce: Nonce | rpc.Error | None = None,
                    refund: Wei = Wei(0)) -> TxParams | rpc.Error | None:
        if nonce is None:
            self._progress('Fetching withdrawal transaction nonce')
            nonce = self.connection.nonce(withdrawer)
            if rpc.is_error(nonce):
                log.error(self.tag, f'withdraw_tx(withdrawer={withdrawer}, recipient={recipient}), failed to get withdrawer nonce, RPC error: {nonce.code.value}')
                return None
        tx: TxParams = {
            'from'   : withdrawer,
            'chainId': self.chain.value,
            'nonce'  : Nonce(nonce),
        }
        self._progress('Building withdrawal transaction')
        built_tx: TxParams | rpc.Error = rpc.contract_build_tx(
            self.proxy_contract, 'withdraw', tx,
            self.deployment_address,
            HexBytes.fromhex(zk_proof['solidity']['proof'][2:] if zk_proof['solidity']['proof'].startswith('0x') else zk_proof['solidity']['proof']),
            merkle_proof.root,
            note.nullifier_hash,
            recipient,
            Web3.to_checksum_address('0x0000000000000000000000000000000000000000'),  # Relayer address
            Wei(0),  # Relayer fee
            refund,
        )
        if rpc.is_error(built_tx):
            log.error(self.tag, f'withdraw_tx(withdrawer={withdrawer}, recipient={recipient}), failed to build transaction, RPC error: {built_tx}')
        return built_tx

    # Withdraw
    # @param    note            The note to withdraw
    # @param    recipient       Address to receive the withdrawal
    # @param    key_or_relayer  The private key to pay the gas fee, or the URL of the relayer
    # @param    nonce           The nonce of the withdrawer address, if None and not using relayer, will be queried from the blockchain
    # @param    refund          Amount to send to the recipient, make sure the `key_or_relayer` is a key and has enough balance
    # @param    can_submit      Optional readiness check immediately before submitting to a relayer
    # @return   HexBytes of transaction hash on success, None if failed
    def withdraw(self,
                 note: Note,
                 recipient: ChecksumAddress,
                 key_or_relayer: Key | str,
                 nonce: Nonce | rpc.Error | None = None,
                 refund: Wei = Wei(0),
                 can_submit: Callable[[], bool] | None = None) -> HexBytes | None:
        if self.sync_only:
            log.error(self.tag, f'withdraw(to={recipient}), not available in sync-only mode')
            return None
        key        : Key | None = None
        relayer_url: str | None = None
        if isinstance(key_or_relayer, Key):
            key = key_or_relayer
        elif isinstance(key_or_relayer, str):
            relayer_url = key_or_relayer
        else:
            log.error(self.tag, f'withdraw(to={recipient}), key_or_relayer is either a Key or a relayer URL string, value: {key_or_relayer}')
            return None
        use_relayer: bool = relayer_url is not None and relayer_url.startswith('http')
        total_wei  : Wei  = unit_to_wei(self.unit, self.meta.decimals[self.symbol])

        relayer_address: ChecksumAddress = Web3.to_checksum_address('0x0000000000000000000000000000000000000000')
        relayer_fee    : Wei             = Wei(0)
        if use_relayer:
            quote = relayer.quote(relayer_url, recipient, chain=self.chain, symbol=self.symbol, unit=self.unit,
                                  deployment_address=self.deployment_address, total_wei=total_wei,
                                  decimals=self.meta.decimals[self.symbol], refund=refund,
                                  get_gas_price=self.connection.gas_price, tag=self.tag, on_progress=self._progress)
            if quote is None:
                return None
            relayer_address, relayer_fee = quote

        # Merkle proof and ZK proof
        prepared: tuple[MerkleProof, dict] | None = self.prepare_withdraw(note, recipient, relayer_address, relayer_fee, refund)
        if prepared is None:
            return None
        merkle_proof: MerkleProof
        zk_proof    : dict
        merkle_proof, zk_proof = prepared

        # Make withdrawal via relayer
        if use_relayer:
            return relayer.submit(relayer_url, recipient, relayer_address, zk_proof,
                                  deployment_address=self.deployment_address, symbol=self.symbol, unit=self.unit,
                                  tag=self.tag, on_progress=self._progress, can_submit=can_submit)

        # Otherwise, make withdrawal directly
        if nonce is None:
            nonce = self.connection.nonce(key.eth_address())
            if rpc.is_error(nonce):
                log.error(self.tag, f'withdraw(withdrawer={key.eth_address()}, recipient={recipient}), failed to get withdrawer nonce, RPC error: {nonce.code.value}')
                return None
        # Build
        built_tx: TxParams | rpc.Error | None = self.withdraw_tx(note, key.eth_address(), recipient, merkle_proof, zk_proof, nonce, refund)
        tx_hash = wallet.send(self.connection, built_tx, key, self.tag,
                              f'withdraw(withdrawer={key.eth_address()}, recipient={recipient})')
        if tx_hash is None:
            return None
        log.info(self.tag, f'Withdraw {self.unit.value} {self.symbol.value.upper()} succeed, tx hash: {tx_hash.to_0x_hex()}')
        return tx_hash

    def start_sync(self, *, start_gate: threading.Event | None = None) -> bool:
        contract    : ChecksumAddress = self.meta.deployment[self.symbol][self.unit][0]
        latest_block: int | None      = self.db.get_latest_block_number()
        if latest_block is None:
            log.error(self.tag, 'start_sync(), failed to get latest block number from database')
            return False
        if latest_block > self.meta.deployment[self.symbol][self.unit][1]:
            latest_block += 1
        return self.poller.start(contract, latest_block, start_gate=start_gate)

    def stop_sync(self) -> None:
        self.poller.stop()

    def on_first_catchup(self) -> None:
        with self.mutex:
            self.catchup = True
            for h in self.handlers:
                h.on_first_catchup(self.chain, self.symbol, self.unit)

    def on_sync(self, block_from: int, block_to: int, deposits: list[EventDeposit], withdrawals: list[EventWithdraw]) -> None:
        with self.mutex:
            if not self.db.add_synchronized(block_to, deposits, withdrawals):
                raise RuntimeError(f'on_sync() failed to add synchronized data to database')
            if not self.sync_only:
                for e in deposits:
                    if not self.tree.add(e.commitment):
                        raise RuntimeError('on_sync() failed to update merkle tree')
            self.last_update_sec = helper_func.unix_timestamp()
            for h in self.handlers:
                h.on_sync(block_from, block_to, deposits, withdrawals)

    def on_latest_block(self, block_number: int) -> None:
        with self.mutex:
            for h in self.handlers:
                h.on_latest_block(block_number)
