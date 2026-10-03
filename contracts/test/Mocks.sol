// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract TestToken is ERC20 {
    bool private immutable noReturn;
    bool private immutable zeroFirst;

    constructor(bool noReturn_, bool zeroFirst_) ERC20("Test", "TEST") {
        noReturn = noReturn_;
        zeroFirst = zeroFirst_;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function approve(address spender, uint256 amount) public override returns (bool) {
        require(!zeroFirst || amount == 0 || allowance(msg.sender, spender) == 0, "Zero first");
        bool result = super.approve(spender, amount);
        if (noReturn) assembly { return(0, 0) }
        return result;
    }

    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        bool result = super.transferFrom(from, to, amount);
        if (noReturn) assembly { return(0, 0) }
        return result;
    }
}

contract TestDecimalsToken is TestToken {
    uint8 private immutable places;

    constructor(uint8 places_) TestToken(false, false) {
        places = places_;
    }

    function decimals() public view override returns (uint8) {
        return places;
    }
}

contract TestReverter {
    error FixtureError(uint256 value);

    function failString() external pure {
        revert("Fixture revert");
    }

    function failPanic(uint256 value) external pure returns (uint256) {
        return value + 1;
    }

    function failCustom() external pure {
        revert FixtureError(7);
    }
}

contract TestMulticall3 {
    struct Call3Value {
        address target;
        bool allowFailure;
        uint256 value;
        bytes callData;
    }

    struct Result {
        bool success;
        bytes returnData;
    }

    function aggregate3Value(Call3Value[] calldata calls) external payable returns (Result[] memory results) {
        results = new Result[](calls.length);
        uint256 total;
        for (uint256 i; i < calls.length; ++i) {
            total += calls[i].value;
            (results[i].success, results[i].returnData) = calls[i].target.call{value: calls[i].value}(calls[i].callData);
            require(results[i].success || calls[i].allowFailure, "Call failed");
        }
        require(total == msg.value, "Incorrect value");
    }
}

contract TestPool {
    using SafeERC20 for IERC20;
    uint256 public immutable denomination;
    IERC20 public immutable token;
    uint32 public count;
    mapping(bytes32 => bool) public commitments;
    event Deposit(bytes32 indexed commitment, uint32 leafIndex, uint256 timestamp);

    constructor(IERC20 token_, uint256 amount) {
        token = token_;
        denomination = amount;
    }

    function deposit(bytes32 commitment) external payable {
        require(!commitments[commitment], "Duplicate commitment");
        if (address(token) == address(0)) {
            require(msg.value == denomination, "Incorrect native amount");
        } else {
            require(msg.value == 0, "Unexpected native amount");
            token.safeTransferFrom(msg.sender, address(this), denomination);
        }
        commitments[commitment] = true;
        emit Deposit(commitment, count++, block.timestamp);
    }
}

contract TestProxy {
    using SafeERC20 for IERC20;
    mapping(address => bool) public supported;

    function addPool(address pool) external {
        supported[pool] = true;
        IERC20 token = TestPool(pool).token();
        if (address(token) != address(0)) token.forceApprove(pool, type(uint256).max);
    }

    function deposit(address instance, bytes32 commitment, bytes calldata) external payable {
        require(supported[instance], "Unsupported pool");
        TestPool pool = TestPool(instance);
        IERC20 token = pool.token();
        if (address(token) != address(0)) {
            token.safeTransferFrom(msg.sender, address(this), pool.denomination());
        }
        pool.deposit{value: msg.value}(commitment);
    }
}
