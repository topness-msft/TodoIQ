/* Minimal, escaped Markdown rendering for Cowork findings and draft previews. */

function _stripContextRefs(text) {
    return String(text || '').replace(/\[([^\]]*)\]\(context:[^)]*\)/g, '$1');
}

function _coworkInlineMarkdown(text) {
    var escaped = String(text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

    escaped = escaped.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(_, label, url) {
        if (!/^https:\/\//i.test(url)) return label;
        return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">'
            + label + '</a>';
    });
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    return escaped;
}

function renderCoworkMarkdown(text) {
    var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    var html = [];
    var paragraph = [];
    var inList = false;

    function flushParagraph() {
        if (!paragraph.length) return;
        html.push('<p>' + paragraph.map(_coworkInlineMarkdown).join('<br>') + '</p>');
        paragraph = [];
    }
    function closeList() {
        if (!inList) return;
        html.push('</ul>');
        inList = false;
    }

    lines.forEach(function(line) {
        var heading = line.match(/^##\s+(.+)$/);
        var bullet = line.match(/^\s*[-*]\s+(.+)$/);
        if (heading) {
            flushParagraph();
            closeList();
            html.push('<h2>' + _coworkInlineMarkdown(heading[1]) + '</h2>');
        } else if (bullet) {
            flushParagraph();
            if (!inList) {
                html.push('<ul>');
                inList = true;
            }
            html.push('<li>' + _coworkInlineMarkdown(bullet[1]) + '</li>');
        } else if (!line.trim()) {
            flushParagraph();
            closeList();
        } else {
            closeList();
            paragraph.push(line);
        }
    });
    flushParagraph();
    closeList();
    return html.join('');
}
