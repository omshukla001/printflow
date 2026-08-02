// QR Scanner using html5-qrcode
(function() {
    const container = document.getElementById('scanner-container');
    const resultDiv = document.getElementById('scan-result');
    const manualForm = document.getElementById('manual-form');
    const manualToken = document.getElementById('manual-token');

    if (!container) return;

    let scanner = null;
    let isProcessing = false;

    function initScanner() {
        if (typeof Html5Qrcode === 'undefined') {
            container.innerHTML = '<p class="text-muted text-center">QR scanner library not loaded. Use manual entry below.</p>';
            return;
        }

        scanner = new Html5Qrcode('scanner-container');

        scanner.start(
            { facingMode: 'environment' },
            { fps: 10, qrbox: { width: 250, height: 250 } },
            onScanSuccess,
            () => {} // ignore scan failures
        ).catch(err => {
            container.innerHTML = '<p class="text-muted text-center">Camera not available. Use manual entry below.</p>';
            console.error('Scanner error:', err);
        });
    }

    function onScanSuccess(decodedText) {
        if (isProcessing) return;
        isProcessing = true;

        // Brief pause to prevent rapid rescans
        if (scanner) {
            scanner.pause(true);
        }

        processToken(decodedText);
    }

    function csrfToken() {
        var el = document.querySelector('meta[name="csrf-token"]');
        return el ? el.getAttribute('content') : '';
    }

    function processToken(token) {
        showResult('Processing...', 'info');

        fetch('/admin/scan/process', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify({ qr_token: token })
        })
        .then(res => res.json())
        .then(data => {
            if (data.ok) {
                showResult(
                    `Prioritized ${data.jobs_prioritized} job(s) for ${data.user}`,
                    'success'
                );
            } else {
                showResult(data.error || 'Unknown error', 'error');
            }
        })
        .catch(err => {
            showResult('Network error: ' + err.message, 'error');
        })
        .finally(() => {
            setTimeout(() => {
                isProcessing = false;
                if (scanner) {
                    try { scanner.resume(); } catch(e) {}
                }
            }, 3000);
        });
    }

    function showResult(message, type) {
        resultDiv.textContent = message;
        resultDiv.className = 'scan-result ' + type;
        resultDiv.classList.remove('hidden');
    }

    // Manual form submission
    if (manualForm) {
        manualForm.addEventListener('submit', e => {
            e.preventDefault();
            const token = manualToken.value.trim();
            if (token) {
                processToken(token);
                manualToken.value = '';
            }
        });
    }

    initScanner();
})();
