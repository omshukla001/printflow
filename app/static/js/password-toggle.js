// Show/hide toggle for every password input on the page.
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('input[type="password"]').forEach(function (input) {
        const wrap = document.createElement('div');
        wrap.className = 'password-field';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'password-toggle';
        btn.setAttribute('aria-label', 'Show password');
        btn.setAttribute('aria-pressed', 'false');
        btn.textContent = 'Show';
        wrap.appendChild(btn);

        btn.addEventListener('click', function () {
            const shown = input.type === 'text';
            input.type = shown ? 'password' : 'text';
            btn.textContent = shown ? 'Show' : 'Hide';
            btn.setAttribute('aria-label', shown ? 'Show password' : 'Hide password');
            btn.setAttribute('aria-pressed', shown ? 'false' : 'true');
            input.focus();
        });
    });
});
