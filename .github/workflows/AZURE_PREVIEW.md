# Manually approved Azure preview

Automatic PR CI no longer authenticates to Azure. Tests, image builds, smoke tests,
source scans and dependency/image scans remain automatic. Required CI gate covers
all original unprivileged jobs and the new workflow security tests. The Subscription branch already has an aggregate gate; this change extends it with
workflow-safety and preserves Bicep, integration, contract, Producer SDK, service,
Docker and scanner checks, including reusable workflow_call validation for manual CD.

## Trust and authorization

Only workflow_dispatch from refs/heads/main in RameshMalar/scalable-incident-platform
is eligible. Workflow commands and the validator come from that dispatch's immutable
main SHA. source_sha must be a full lowercase 40-character commit SHA accessible in
the explicitly identified same-repository PR; refs and abbreviated SHAs fail.
The required pr_number is a positive decimal PR number, not a URL or free-form ref.
Before checkout, GitHub API checks require an open, unmerged PR with head and base
repositories both equal to RameshMalar/scalable-incident-platform, base branch main,
and head SHA exactly source_sha. Forks, closed PRs, mismatches and API denial fail closed.
The verified PR number, repository, base branch and SHA are bound into the manifest.
After download, provenance is queried again before Azure login: moved or closed PRs
require a new dispatch for the newly reviewed SHA. No candidate scripts/actions run.
review_reference is a human evidence label, never authorization or proof of CI success.
Protected-environment reviewer approval remains an independent authorization control.

Candidate infra files are data only. No candidate script, action, submodule or LFS
filter is executed. A credential-free compilation job uses pinned Bicep v0.47.16,
--no-restore, rejects candidate compiler configuration and symlinks, and blocks linked
templates and deployment scripts. Core contains a Log Analytics listKeys expression;
the preview identity must not gain listKeys permission to resolve it. Unresolved
expressions or provider failures are limitations to report, not reasons to broaden RBAC. It strips ARM outputs.
Compilation may download the pinned compiler; it does not authenticate to Azure.

An attempt-specific artifact (reviewed-azure-template-${{ github.run_attempt }}) contains exactly core.json and manifest.json. The approved job
retrieves the immutable artifact ID from this same run, verifies filenames, size,
regular files, source SHA, review reference and independently recorded template
SHA-256. Nothing selects an artifact from another run. Artifacts expire after one day;
late approval must fail rather than fall back to a different artifact.
A full rerun uploads a distinct artifact for its run_attempt. Download remains by the
returned immutable artifact ID, never a name search. Require a fresh environment
approval for reruns where GitHub supports it; do not assume failed-job or individual-job
reruns automatically repeat approval. If fresh approval cannot be guaranteed, use a new
manual dispatch and review that new run instead. Artifact deletion/expiration and run
cancellation fail closed; no alternate artifact or production credential is selected.

## GitHub administrator prerequisites

Create the existing environment azure-preview before dispatch. Require authorized
reviewers, prevent self-review, disable administrator bypass, and select exactly one
deployment branch policy: branch main (no wildcard and no tags). Protection is read
through the GitHub API before compilation and again before Azure login. A missing
setting or denied API read fails closed. Verify the repository plan supports all
these protections; do not create an unprotected substitute or use a PAT workaround.
The repository must remain private because the compiled template artifact may contain
configuration details. Reviewers approve the run's exact source SHA, review reference,
compiled hash, target scope and dedicated identity configuration.

Protect main and .github/workflows plus scripts/validate-azure-preview.py with review
and ownership rules. Restrict who can dispatch. A GitHub administrator able to change
these controls remains trusted. Branch policy and workflow conditions both restrict
main; Azure federation must independently bind authentication to azure-preview.

Set environment variables (not production fallbacks):

- AZURE_PREVIEW_CLIENT_ID: dedicated preview identity application/client ID.
- AZURE_PREVIEW_TENANT_ID and AZURE_PREVIEW_SUBSCRIPTION_ID: approved target directory/subscription.
- AZURE_PREVIEW_RESOURCE_GROUP: approved resource group.
- AZURE_PREVIEW_DEVELOPER_PRINCIPAL_IDS: explicit JSON UUID array, including [] when reviewed.
- AZURE_PREVIEW_CONFIGURATION_APPROVED: literal true after federation/RBAC review.
- AZURE_CLIENT_ID: existing production identity ID, used only to reject identity reuse.

Only preview receives id-token: write. All other jobs retain read-only GitHub content
permissions; protection reads require actions: read and PR reads require pull-requests: read. The preview job uses no production
Azure credentials. The configuration-approved variable is an administrator attestation,
not an Azure role-assignment verification.

## Azure administrator prerequisites

Provision a separate preview identity, not the deployment identity. Its federation must
trust issuer https://token.actions.githubusercontent.com, audience api://AzureADTokenExchange,
and subject repo:RameshMalar/scalable-incident-platform:environment:azure-preview.
Remove legacy pull_request trust from the production deployment identity and review
all other credentials for alternate access. Do not change production trust blindly:
coordinate the protected production deployment configuration separately.

Use resource-group-scoped read permissions and a reviewed custom role for the minimum
necessary deployment preview/read operations. Evaluate ProviderNoRbac with Azure CLI
2.76+; the workflow checks that minimum and supplies the explicit validation level.
Do not grant Contributor, Owner, broad deployments/*, roleAssignments/write,
listKeys actions or data-plane permissions to make a preview pass. Reader alone is
not assumed sufficient for the what-if API. An Azure administrator must verify the
exact required API operations and confirm real deployment/resource mutation is denied.
This local correction does not create identities, federation or RBAC.

## CI preservation and shallow checkout

The safety test uses frozen semantic SHA-256 contracts of every unprivileged job and
trigger at the verified base. Command, matrix, dependency or scanner changes still fail
preservation checks. No historical Git objects are required at runtime. A local regression
runs the preservation assertion in a depth-one checkout with the draft files overlaid;
contract mutation tests confirm command changes are detected.

## Output and limitations

The command is az deployment group what-if, never deployment create, confirm-with-what-if,
or a data-plane operation. Raw output/errors stay in private runner temporary files;
no raw JSON, resource identifiers, property values or exception body is logged/uploaded.
The summary contains only reviewed SHA/hash and change-type counts. Compile diagnostics
are likewise withheld. The immutable template artifact is private but accessible to
repository readers: never embed secrets in candidate templates or review references.

What-if is advisory: no runtime proof, delivery test, permission enforcement test, or
proof of safe deployment. ProviderNoRbac omits write-permission verification. Unsupported,
Ignore, expression resolution and provider validation limits still apply; reviewers must
inspect candidate source independently. This intentionally previews core only, not service
templates whose parameters depend on deployed outputs. If diagnostics are needed, request
a separate approved inspection; never enable debug or echo raw results casually.

## Subscription integration and approval gates

This selective adaptation starts from Subscription commit
58b84fef44686fb947b0d77019d54cfbfdf9ef23 and security reference
cc7730141a76822761c04300b1642ea1e1fb52f6. It removes only the privileged
PR infra job. CD stays byte-for-byte unchanged and workflow_dispatch-only.
No business logic, dependency specifications, Dockerfiles or Bicep resources change.

1. Obtain separate approval to push/open the reviewed Subscription PR. Before doing
   so, administrators must contain legacy PR OIDC trust in the production identity
   and inspect main's actual workflows: this local fix cannot secure other PRs.
2. Review cloud-free CI and the expanded gate. Before an approved merge, verify
   legacy main CD is disabled and the resulting CD is manual-only. Local files do
   not alter GitHub settings or production identity federation.
3. Bootstrap the reviewed preview workflow/validator onto protected main through
   an independently approved review. Main-only dispatch cannot use a workflow that
   exists only on this feature branch. An open same-repository PR with the exact
   candidate head SHA remains required for preview.
4. Administrators provision/verify azure-preview protection, dedicated federation,
   least-privilege RBAC and all listed variables. Until then, preview fails closed.
5. Dispatch preview only under explicit authorization for the exact SHA and scope;
   a different authorized reviewer approves the compiled hash before Azure login.
6. Production publication and manual CD require separate scope-specific approvals.
   Existing CD preparation publishes five workload images by default and adds
   the dashboard image only for an explicit dashboard release; it is not a
   Subscription-only deployment. Optional Worker routing requires its separate
   production approval and rollback configuration.

These are prerequisites, not actions performed by this local integration.

## Disable and rollback

Disable azure-preview.yml and revoke preview federation to stop previews. Do not restore
PR Azure authentication. If reverting workflow code is necessary, retain credential
containment and main CD disabling so a revert cannot deploy. No resource data was changed
by preview, so there is no infrastructure/data rollback to perform. Reverting this security
correction must not silently re-enable the removed privileged PR job.

PR API rechecks are point-in-time: a PR can move immediately afterward, but the compiled
template and immutable SHA never change. Main is likewise pinned to the dispatch SHA.
Approvers must review that exact revision; branch protection and restricted dispatch
permissions remain administrator prerequisites.
