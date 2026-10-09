content = '''{% extends "base.html" %}
{% block body %}
<div class="pagehead">
  <div>
    <h1>All Scripts</h1>
    <div class="small mute">View all generated and imported scripts.</div>
  </div>
</div>

<div class="card">
  <div class="tw">
    <table class="stack">
      <thead>
        <tr>
          <th>ID</th>
          <th>Language</th>
          <th>Domain</th>
          <th>Subdomain</th>
          <th>Status</th>
          <th>Source</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for s in rows %}
        <tr>
          <td><b>{{ s.code }}</b></td>
          <td>{{ s.lang_name }}</td>
          <td>{{ s.domain }}</td>
          <td>{{ s.subdomain }}</td>
          <td>
            <span class="pill {{ 'ok' if s.status == 'READY' or s.status == 'APPROVED' else 'warn' }}">
              {{ s.status }}
            </span>
          </td>
          <td class="small">{{ s.source }}</td>
          <td><a href="/admin/script/{{ s.id }}" class="btn sm">View</a></td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="mute">No scripts found.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
'''
open('app/templates/admin_scripts.html', 'w', encoding='utf-8').write(content)
print("Done")
