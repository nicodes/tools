"""Keep static Godot deployment authority in its source cd.yml caller."""
import json
import godot_static
import godot_ci


def jobs(root,config,tools):
    from delivery_workflows import declaration
    data=declaration(root,config)
    static=godot_static.declaration(root,config)
    if static is None:raise ValueError('static workflow composition requires its explicit native delivery mode')
    if data['publish_runner']!=data['deploy_runner'] or data['publish_timeout']!=data['deploy_timeout']:
        raise ValueError('static publication and deployment preserve the existing combined runner and total deadline')
    ref=data['komizo_ref']
    condition="github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && (inputs.source == '' || inputs.source == github.sha)"
    lines=['  cd:','    name: Deploy accepted static game','    needs: validate','    if: '+condition,
      '    runs-on: '+data['deploy_runner'],'    timeout-minutes: '+str(data['deploy_timeout']),
      '    environment: production','    concurrency:','      group: deploy-production','      cancel-in-progress: false',
      '    permissions:','      contents: read','      actions: read','      packages: write','      attestations: read','      id-token: write',
      '    outputs:','      release-id: ${{ steps.delivery.outputs.release-id }}',
      '    steps:','      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
      '        with:','          fetch-depth: 0','          persist-credentials: false',
      '      - uses: nicodes/komizo-actions/godot-static-delivery@'+ref,'        id: delivery','        with:',
      '          tools-ref: '+tools,'          source: ${{ github.sha }}','        env:',
      '          KOMIZO_APP_NAME: ${{ vars.KOMIZO_APP_NAME }}',
      '          KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}',
      '          KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}',
      '          KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}']
    if static['production']:
        entries=godot_ci.plan(root,config)['production']['include']
        if len(entries)!=1:raise ValueError('static post-deployment verification requires one real fixed-origin production gate')
        entry=entries[0]
        lines+=['  public_verification:','    name: Verify exact ordinary public release','    needs: cd','    if: '+condition,
          '    uses: nicodes/komizo-actions/.github/workflows/cookbook-godot-production.yml@'+ref,
          '    permissions:','      contents: read','    with:','      tools-ref: '+tools,'      komizo-ref: '+ref,
          '      source: ${{ github.sha }}','      release-id: ${{ needs.cd.outputs.release-id }}',
          '      runner: '+entry['runner'],'      timeout-minutes: '+str(entry['timeout_minutes'])]
    lines+=['  report:','    needs: [validate, cd'+(', public_verification' if static['production'] else '')+']',
      '    if: always() && !cancelled() && ('+condition+')',
      '    uses: nicodes/komizo-actions/.github/workflows/cookbook-report.yml@'+ref,
      '    permissions:','      contents: read','      issues: write','    with:','      tools-ref: '+tools,'      komizo-ref: '+ref,
      '      validation: ${{ needs.validate.result }}','      publication: ${{ needs.cd.result }}',
      "      deployment: ${{ needs.cd.result == 'success' && "+("needs.public_verification.result == 'success'" if static['production'] else 'true')+" && 'success' || 'failure' }}",
      '      deployment-url: '+json.dumps(data['notification_url']),'    secrets:',
      '      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}']
    return '\n'.join(lines)+'\n'
