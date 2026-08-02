// Queue drag-and-drop reordering
(function() {
    const list = document.getElementById('queue-list');
    if (!list) return;

    let dragItem = null;

    list.querySelectorAll('.queue-item').forEach(item => {
        const handle = item.querySelector('.queue-drag-handle');
        if (!handle) return;

        handle.addEventListener('mousedown', () => item.draggable = true);
        handle.addEventListener('touchstart', () => item.draggable = true);

        item.addEventListener('dragstart', e => {
            dragItem = item;
            item.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });

        item.addEventListener('dragend', () => {
            item.classList.remove('dragging');
            item.draggable = false;
            dragItem = null;
            saveOrder();
        });

        item.addEventListener('dragover', e => {
            e.preventDefault();
            if (!dragItem || dragItem === item) return;
            const rect = item.getBoundingClientRect();
            const mid = rect.top + rect.height / 2;
            if (e.clientY < mid) {
                list.insertBefore(dragItem, item);
            } else {
                list.insertBefore(dragItem, item.nextSibling);
            }
        });
    });

    function csrfToken() {
        var el = document.querySelector('meta[name="csrf-token"]');
        return el ? el.getAttribute('content') : '';
    }

    function saveOrder() {
        const items = list.querySelectorAll('.queue-item');
        const order = Array.from(items).map(item => parseInt(item.dataset.jobId));

        fetch('/admin/queue/reorder', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify({ order: order })
        }).catch(err => console.error('Reorder failed:', err));
    }
})();
