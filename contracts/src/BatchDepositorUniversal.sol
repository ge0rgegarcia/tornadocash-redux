// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BatchDepositorBase, ITornadoPool} from "./BatchDepositorBase.sol";

contract BatchDepositorUniversal is BatchDepositorBase {
    constructor(address routerAddress) BatchDepositorBase(routerAddress) {}

    function deposit(TokenDeposits[] calldata groups) external payable onlyOwnerOnce {
        uint256 nativeValue;
        for (uint256 i; i < groups.length; ++i) {
            TokenDeposits memory group = groups[i];
            if (group.token != address(0)) {
                _depositToken(IERC20(group.token), group.deposits);
                continue;
            }
            for (uint256 j; j < group.deposits.length; ++j) {
                Deposit memory item = group.deposits[j];
                uint256 amount = ITornadoPool(item.instance).denomination();
                nativeValue += amount;
                router.deposit{value: amount}(item.instance, item.commitment, "");
            }
        }
        require(msg.value == nativeValue, "Incorrect value");
    }
}
