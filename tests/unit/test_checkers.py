# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import pytest
from flexmock import flexmock

from packit.config.job_config import JobType, JobConfigTriggerType
from packit_service.config import ServiceConfig
from packit_service.models import CoprBuildTargetModel
from packit_service.worker.checker.koji import PermissionOnKoji
from packit_service.worker.checker.vm_image import (
    IsCoprBuildForChrootOk,
    HasAuthorWriteAccess,
)
from packit_service.worker.events import (
    PullRequestGithubEvent,
)
from packit_service.worker.events.event import EventData
from packit_service.worker.events.github import (
    PushGitHubEvent,
    PullRequestCommentGithubEvent,
)
from packit_service.worker.events.gitlab import MergeRequestGitlabEvent, PushGitlabEvent
from packit_service.worker.events.pagure import PushPagureEvent
from packit_service.worker.helpers.build.koji_build import KojiBuildJobHelper
from packit_service.worker.mixin import ConfigFromEventMixin


def construct_dict(event, action=None, git_ref=None):
    return {
        "event_type": event,
        "actor": "bfu",
        "project_url": "some_url",
        "git_ref": git_ref,
        "action": action,
    }


@pytest.mark.parametrize(
    "success, event, is_scratch, can_merge_pr, trigger",
    (
        pytest.param(
            False,
            construct_dict(event=MergeRequestGitlabEvent.__name__, action="closed"),
            True,
            True,
            JobConfigTriggerType.pull_request,
            id="closed MRs are ignored",
        ),
        pytest.param(
            False,
            construct_dict(event=PushGitHubEvent.__name__),
            True,
            None,
            JobConfigTriggerType.commit,
            id="GitHub push to non-configured branch is ignored",
        ),
        pytest.param(
            False,
            construct_dict(event=PushGitlabEvent.__name__),
            True,
            None,
            JobConfigTriggerType.commit,
            id="GitLab push to non-configured branch is ignored",
        ),
        pytest.param(
            False,
            construct_dict(event=PushPagureEvent.__name__),
            True,
            None,
            JobConfigTriggerType.commit,
            id="Pagure push to non-configured branch is ignored",
        ),
        pytest.param(
            True,
            construct_dict(event=PushPagureEvent.__name__, git_ref="release"),
            True,
            None,
            JobConfigTriggerType.commit,
            id="Pagure push to configured branch is not ignored",
        ),
        pytest.param(
            False,
            construct_dict(event=PullRequestGithubEvent.__name__),
            True,
            False,
            JobConfigTriggerType.pull_request,
            id="Permissions on GitHub",
        ),
        pytest.param(
            False,
            construct_dict(event=MergeRequestGitlabEvent.__name__),
            True,
            False,
            JobConfigTriggerType.pull_request,
            id="Permissions on GitLab",
        ),
        pytest.param(
            False,
            construct_dict(event=MergeRequestGitlabEvent.__name__),
            False,
            True,
            JobConfigTriggerType.pull_request,
            id="Non-scratch builds are prohibited",
        ),
        pytest.param(
            True,
            construct_dict(event=PullRequestGithubEvent.__name__),
            True,
            True,
            JobConfigTriggerType.pull_request,
            id="PR from GitHub shall pass",
        ),
        pytest.param(
            True,
            construct_dict(event=MergeRequestGitlabEvent.__name__),
            True,
            True,
            JobConfigTriggerType.pull_request,
            id="MR from GitLab shall pass",
        ),
    ),
)
def test_koji_permissions(success, event, is_scratch, can_merge_pr, trigger):
    package_config = flexmock(jobs=[])
    job_config = flexmock(
        type=JobType.upstream_koji_build,
        scratch=is_scratch,
        trigger=trigger,
        targets={"fedora-37"},
        branch="release",
    )

    git_project = flexmock(
        namespace="packit",
        repo="ogr",
        default_branch="main",
    )
    git_project.should_receive("can_merge_pr").and_return(can_merge_pr)
    flexmock(ConfigFromEventMixin).should_receive("project").and_return(git_project)

    db_trigger = flexmock(job_config_trigger_type=trigger)
    flexmock(EventData).should_receive("db_trigger").and_return(db_trigger)

    if not success:
        flexmock(KojiBuildJobHelper).should_receive("report_status_to_all")

    checker = PermissionOnKoji(package_config, job_config, event)

    assert checker.pre_check() == success


@pytest.mark.parametrize(
    "success, copr_builds, error_msg",
    (
        pytest.param(
            True,
            [
                flexmock(
                    project_name="knx-stack",
                    owner="mmassari",
                    target="fedora-36-x86_64",
                    status="success",
                ),
            ],
            None,
            id="A successful Copr build for project found",
        ),
        pytest.param(
            False,
            [
                flexmock(
                    project_name="knx-stack",
                    owner="mmassari",
                    target="fedora-36-x86_64",
                    status="failed",
                    built_packages=[],
                ),
            ],
            (
                "No successful Copr build found for project mmassari/knx-stack"
                " commit 1 and chroot (target) fedora-36-x86_64"
            ),
            id="No successful copr build for project found",
        ),
        pytest.param(
            False,
            [
                flexmock(
                    project_name="knx-stack",
                    owner="mmassari",
                    target="fedora-38-arm_32",
                    status="failed",
                    built_packages=[],
                ),
            ],
            (
                "No successful Copr build found for project mmassari/knx-stack"
                " commit 1 and chroot (target) fedora-36-x86_64"
            ),
            id="No copr build for target found",
        ),
        pytest.param(
            False,
            [],
            "No Copr build found for commit sha 1",
            id="No copr build found",
        ),
    ),
)
def test_vm_image_is_copr_build_ok_for_chroot(
    fake_package_config_job_config_project_db_trigger, success, copr_builds, error_msg
):
    package_config, job_config, _, _ = fake_package_config_job_config_project_db_trigger

    flexmock(CoprBuildTargetModel).should_receive("get_all_by_commit").and_return(
        copr_builds
    )

    checker = IsCoprBuildForChrootOk(
        package_config,
        job_config,
        {"event_type": PullRequestCommentGithubEvent.__name__, "commit_sha": "1"},
    )

    if error_msg:
        flexmock(checker).should_receive("report_pre_check_failure").with_args(
            error_msg
        ).once()

    assert checker.pre_check() == success


@pytest.mark.parametrize(
    "has_write_access, result",
    (
        pytest.param(
            True,
            True,
            id="Author has write access",
        ),
        pytest.param(
            False,
            False,
            id="Author has not write access",
        ),
    ),
)
def test_vm_image_has_author_write_access(
    fake_package_config_job_config_project_db_trigger, has_write_access, result
):
    package_config, job_config, _, _ = fake_package_config_job_config_project_db_trigger

    actor = "maja"
    project_url = "just an url"
    checker = HasAuthorWriteAccess(
        package_config,
        job_config,
        {
            "event_type": PullRequestCommentGithubEvent.__name__,
            "actor": actor,
            "project_url": project_url,
        },
    )

    flexmock(ServiceConfig).should_receive("get_project").with_args(
        url=project_url
    ).and_return(
        flexmock(repo="repo", namespace="ns")
        .should_receive("has_write_access")
        .with_args(user=actor)
        .and_return(has_write_access)
        .mock()
    )

    if not has_write_access:
        flexmock(checker).should_receive("report_pre_check_failure").once()

    assert checker.pre_check() == result
