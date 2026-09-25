from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JulesMode:
    name: str

    pretest: bool
    pretest_fix: bool

    deploy: bool
    deploy_fix: bool

    live_test: bool
    live_fix: bool

    require_plan_approval: bool


MODES = {
    "inspect": JulesMode(
        name="inspect",
        pretest=False,
        pretest_fix=False,
        deploy=False,
        deploy_fix=False,
        live_test=False,
        live_fix=False,
        require_plan_approval=False,
    ),

    "validate": JulesMode(
        name="validate",
        pretest=True,
        pretest_fix=False,
        deploy=False,
        deploy_fix=False,
        live_test=False,
        live_fix=False,
        require_plan_approval=False,
    ),

    "validate_fix": JulesMode(
        name="validate_fix",
        pretest=True,
        pretest_fix=True,
        deploy=False,
        deploy_fix=False,
        live_test=False,
        live_fix=False,
        require_plan_approval=False,
    ),

    "deploy_validate": JulesMode(
        name="deploy_validate",
        pretest=True,
        pretest_fix=False,
        deploy=True,
        deploy_fix=False,
        live_test=True,
        live_fix=False,
        require_plan_approval=False,
    ),

    "deploy_fix": JulesMode(
        name="deploy_fix",
        pretest=True,
        pretest_fix=True,
        deploy=True,
        deploy_fix=True,
        live_test=True,
        live_fix=True,
        require_plan_approval=True,
    ),

    "full": JulesMode(
        name="full",
        pretest=True,
        pretest_fix=True,
        deploy=True,
        deploy_fix=True,
        live_test=True,
        live_fix=True,
        require_plan_approval=True,
    ),
}


def get_mode(
    name: str,
) -> JulesMode:

    try:
        return MODES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown Jules mode: {name}"
        ) from exc
