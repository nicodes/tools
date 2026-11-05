"""Generate thin CLI release callers while retaining their published signer identity."""
from configuration import load, ci_settings
from cli_release import policy

CHECKOUT = 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1'
DOWNLOAD = 'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333'
UPLOAD = 'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'
ATTEST = 'actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8'


def workflow_files(root, revision):
    config = load(root)
    values, settings = policy(config), ci_settings(config)
    text = '''# Generated cookbook caller. Change recipe data and regenerate.
# Attestation stays in this source workflow to preserve published installer trust.
name: Release
'on':
  workflow_dispatch:
    inputs:
      bump:
        description: Version bump
        required: true
        type: choice
        default: patch
        options: [patch, minor, major]
permissions: {}
concurrency:
  group: release-${{ github.repository }}
  cancel-in-progress: false
jobs:
  identity:
    name: Select immutable release identity
    permissions:
      contents: read
    uses: nicodes/tools/.github/workflows/cookbook-cli-identity.yml@PIN
    with:
      tools-ref: PIN
      bump: ${{ inputs.bump }}
  validate:
    name: Validate release source and native archives
    needs: identity
    permissions:
      contents: read
      actions: read
    uses: nicodes/tools/.github/workflows/cookbook-native.yml@PIN
    with:
      tools-ref: PIN
      source: ${{ github.sha }}
      version: ${{ needs.identity.outputs.version }}
      date: ${{ needs.identity.outputs.date }}
      runner: RUNNER
      timeout-minutes: HOST_TIMEOUT
  accept:
    name: Attest accepted source archives
    needs: [identity, validate]
    runs-on: ubuntu-24.04
    timeout-minutes: ACCEPT_TIMEOUT
    permissions:
      contents: read
      actions: read
      attestations: write
      id-token: write
    outputs:
      artifact: ${{ steps.accepted.outputs.artifact-id }}
    env:
      SOURCE_SHA: ${{ github.sha }}
      COOKBOOK_SHA: PIN
      RELEASE_TAG: ${{ needs.identity.outputs.tag }}
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Check out exact source
        uses: CHECKOUT
        with:
          ref: ${{ github.sha }}
          fetch-depth: 0
          persist-credentials: false
      - name: Check out accepted cookbook
        uses: CHECKOUT
        with:
          repository: nicodes/tools
          ref: PIN
          path: _cookbook
          persist-credentials: false
      - name: Bootstrap caller-pinned Python
        uses: ./_cookbook/setup
        with:
          tools: python
      - name: Download this execution's complete source acceptance
        uses: DOWNLOAD
        with:
          name: cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}
          path: .
      - name: Recheck complete native and host proof and exact publication bytes
        run: python3 _cookbook/cookbook/cli_release.py accept --source "$SOURCE_SHA" --tools "$COOKBOOK_SHA" --tag "$RELEASE_TAG"
      - name: Attest build provenance under the published source workflow identity
        uses: ATTEST
        with:
          subject-path: dist/*
      - name: Preserve accepted publication payload and source proof
        id: accepted
        uses: UPLOAD
        with:
          name: accepted-cli-${{ github.sha }}-${{ github.run_attempt }}
          path: |
            dist/
            .artifacts/cookbook-validation.json
          include-hidden-files: true
          if-no-files-found: error
          retention-days: 7
  publish:
    name: Publish immutable release
    needs: [identity, accept]
    permissions:
      contents: write
      actions: read
    uses: nicodes/tools/.github/workflows/cookbook-cli-publish.yml@PIN
    with:
      tools-ref: PIN
      tag: ${{ needs.identity.outputs.tag }}
      artifact-id: ${{ needs.accept.outputs.artifact }}
'''
    for old, new in [('PIN', revision), ('RUNNER', settings['runner']), ('HOST_TIMEOUT', str(values.get('validation_timeout', settings['timeout_minutes']))),
                     ('ACCEPT_TIMEOUT', str(values['acceptance_timeout'])), ('CHECKOUT', CHECKOUT),
                     ('DOWNLOAD', DOWNLOAD), ('UPLOAD', UPLOAD), ('ATTEST', ATTEST)]:
        text = text.replace(old, new)
    return {'.github/workflows/release.yml': text}
