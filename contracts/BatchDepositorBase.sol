// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

interface ITornadoPool {
    function denomination() external view returns (uint256);
    function token() external view returns (IERC20);
}

interface ITornadoProxy {
    function deposit(address instance, bytes32 commitment, bytes calldata encryptedNote) external payable;
}

abstract contract BatchDepositorBase {
    using SafeERC20 for IERC20;

    address public immutable owner;
    ITornadoProxy public immutable router;
    bool public executed;

    struct Deposit {
        address instance;
        bytes32 commitment;
    }

    struct TokenDeposits {
        address token;  // Zero for native deposits in the mixed contract.
        Deposit[] deposits;
    }

    constructor(address routerAddress) {
        require(routerAddress.code.length != 0, "Invalid proxy");
        owner = msg.sender;
        router = ITornadoProxy(routerAddress);
    }

    modifier onlyOwnerOnce() {
        require(msg.sender == owner, "Not owner");
        require(!executed, "Batch already executed");
        executed = true;
        _;
    }

    function _depositToken(IERC20 token, Deposit[] memory deposits) internal {
        uint256 total;
        for (uint256 i; i < deposits.length; ++i) {
            ITornadoPool pool = ITornadoPool(deposits[i].instance);
            require(pool.token() == token, "Token mismatch");
            total += pool.denomination();
        }
        token.safeTransferFrom(msg.sender, address(this), total);
        token.forceApprove(address(router), total);
        for (uint256 i; i < deposits.length; ++i) {
            Deposit memory item = deposits[i];
            router.deposit(item.instance, item.commitment, "");
        }
        token.forceApprove(address(router), 0);
    }
}
