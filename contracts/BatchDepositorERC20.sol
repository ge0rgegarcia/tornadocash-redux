// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BatchDepositorBase} from "./BatchDepositorBase.sol";

contract BatchDepositorERC20 is BatchDepositorBase {
    constructor(address routerAddress) BatchDepositorBase(routerAddress) {}

    function deposit(TokenDeposits[] calldata groups) external onlyOwnerOnce {
        for (uint256 i; i < groups.length; ++i) {
            TokenDeposits memory group = groups[i];
            _depositToken(IERC20(group.token), group.deposits);
        }
    }
}
