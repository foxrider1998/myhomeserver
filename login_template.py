"""HTML Template for Login and Registration Page."""

HTML_LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Masuk - Westpro Home Server</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Outfit:wght@500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <link rel="manifest" href="/manifest.json">
  <meta name="theme-color" content="#0b0c0e">
  <style>
    :root {
      --bg: #08090b;
      --card-bg: rgba(18, 20, 26, 0.88);
      --border: rgba(255, 255, 255, 0.08);
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #f59e0b;
      --accent-hover: #d97706;
      --cyan: #38bdf8;
      --green: #10b981;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background-color: var(--bg);
      background-image: 
        radial-gradient(circle at 15% 20%, rgba(245, 158, 11, 0.08) 0%, transparent 40%),
        radial-gradient(circle at 85% 80%, rgba(56, 189, 248, 0.06) 0%, transparent 45%);
      color: var(--text);
      font-family: 'Inter', -apple-system, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }

    .auth-container {
      width: 100%;
      max-width: 440px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      backdrop-filter: blur(20px);
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 255, 255, 0.05);
      padding: 2.25rem;
      animation: fadeIn 0.4s ease-out;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .brand-header {
      text-align: center;
      margin-bottom: 2rem;
    }

    .brand-icon {
      width: 52px;
      height: 52px;
      background: linear-gradient(135deg, #f59e0b, #ea580c);
      border-radius: 14px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 8px 20px rgba(245, 158, 11, 0.3);
      margin-bottom: 1rem;
    }

    .brand-title {
      font-family: 'Outfit', sans-serif;
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #fff;
    }

    .brand-sub {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-top: 0.25rem;
    }

    /* TABS */
    .auth-tabs {
      display: flex;
      background: #0f1115;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 3px;
      margin-bottom: 1.5rem;
    }

    .auth-tab-btn {
      flex: 1;
      padding: 0.55rem;
      font-size: 0.825rem;
      font-weight: 600;
      background: none;
      border: none;
      color: var(--text-muted);
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .auth-tab-btn.active {
      background: #1e222b;
      color: #fff;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
    }

    /* FORM */
    .form-group {
      margin-bottom: 1.25rem;
    }

    .form-label {
      display: block;
      font-size: 0.8rem;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 0.5rem;
    }

    .form-input {
      width: 100%;
      background: #0d0e12;
      border: 1px solid #2d3340;
      border-radius: 8px;
      padding: 0.75rem 1rem;
      font-size: 0.875rem;
      color: #fff;
      font-family: inherit;
      outline: none;
      transition: all 0.2s;
    }

    .form-input:focus {
      border-color: var(--cyan);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
    }

    .btn-submit {
      width: 100%;
      background: linear-gradient(135deg, #f59e0b, #d97706);
      color: #0b0c0e;
      border: none;
      border-radius: 8px;
      padding: 0.8rem;
      font-size: 0.9rem;
      font-weight: 700;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.2s;
      box-shadow: 0 4px 12px rgba(245, 158, 11, 0.25);
    }

    .btn-submit:hover {
      background: linear-gradient(135deg, #fbbf24, #f59e0b);
      transform: translateY(-1px);
      box-shadow: 0 6px 16px rgba(245, 158, 11, 0.35);
    }

    .btn-submit:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }

    /* QUICK LOGIN PRESET */
    .quick-box {
      margin-top: 1.5rem;
      padding-top: 1.25rem;
      border-top: 1px solid var(--border);
    }

    .quick-title {
      font-size: 0.725rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 0.65rem;
      text-align: center;
    }

    .quick-btn {
      width: 100%;
      background: #141720;
      border: 1px solid rgba(56, 189, 248, 0.3);
      border-radius: 8px;
      padding: 0.65rem 0.85rem;
      color: #38bdf8;
      font-size: 0.8rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      cursor: pointer;
      transition: all 0.2s;
    }

    .quick-btn:hover {
      background: rgba(56, 189, 248, 0.12);
      border-color: #38bdf8;
    }

    .quick-badge {
      background: rgba(245, 158, 11, 0.2);
      color: #fbbf24;
      font-size: 0.68rem;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      font-weight: 700;
    }

    .notice-text {
      margin-top: 1.25rem;
      font-size: 0.75rem;
      color: #64748b;
      text-align: center;
      line-height: 1.4;
    }

    .alert-box {
      padding: 0.65rem 0.85rem;
      border-radius: 6px;
      font-size: 0.775rem;
      margin-bottom: 1rem;
      display: none;
    }

    .alert-error {
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: #f87171;
    }

    .alert-success {
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34d399;
    }
  </style>
</head>
<body>
  <div class="auth-container">
    <div class="brand-header">
      <div class="brand-icon">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5">
          <circle cx="12" cy="12" r="5"/>
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
      </div>
      <div class="brand-title">WESTPRO HOME SERVER</div>
      <div class="brand-sub">Lenovo ThinkCentre M90n &bull; PowMr Inverter Gateway</div>
    </div>

    <!-- TABS -->
    <div class="auth-tabs">
      <button class="auth-tab-btn active" id="tabBtnLogin" onclick="switchAuthTab('login')">Masuk (Login)</button>
      <button class="auth-tab-btn" id="tabBtnRegister" onclick="switchAuthTab('register')">Daftar Akun Baru</button>
    </div>

    <div id="alertBox" class="alert-box"></div>

    <!-- LOGIN FORM -->
    <form id="loginForm" onsubmit="handleLogin(event)" novalidate>
      <div class="form-group">
        <label class="form-label" for="loginEmail">Alamat Email</label>
        <input type="email" id="loginEmail" class="form-input" placeholder="contoh: areskretindo@gmail.com" required autofocus value="areskretindo@gmail.com" oninput="validateEmailLive(this, 'loginEmailErr')">
        <div id="loginEmailErr" style="display: none; color: #f87171; font-size: 0.725rem; margin-top: 0.35rem;">Format email tidak valid (harus mengandung @ dan domain, misal user@domain.com)</div>
      </div>

      <div class="form-group">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <label class="form-label" for="loginPassword" style="margin-bottom: 0;">Kata Sandi (Password)</label>
          <span style="font-size: 0.7rem; color: #94a3b8;">Min. 6 karakter</span>
        </div>
        <div style="position: relative; display: flex; align-items: center;">
          <input type="password" id="loginPassword" class="form-input" placeholder="Masukkan password Anda" required minlength="6" style="padding-right: 2.75rem;" oninput="validatePasswordLive(this, 'loginPassErr')">
          <button type="button" onclick="togglePasswordVisibility('loginPassword', 'eyeIconLogin')" style="position: absolute; right: 0.75rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.25rem; font-size: 0.95rem;" title="Lihat/Sembunyikan Password">
            <span id="eyeIconLogin">👁️</span>
          </button>
        </div>
        <div id="loginPassErr" style="display: none; color: #f87171; font-size: 0.725rem; margin-top: 0.35rem;">Password harus minimal 6 karakter.</div>
      </div>

      <button type="submit" id="btnLoginSubmit" class="btn-submit">Masuk ke Server</button>
    </form>

    <!-- REGISTER FORM -->
    <form id="registerForm" onsubmit="handleRegister(event)" style="display: none;" novalidate>
      <div class="form-group">
        <label class="form-label" for="regName">Nama Lengkap (Opsional)</label>
        <input type="text" id="regName" class="form-input" placeholder="Nama Anda (contoh: Aris / Budi)">
      </div>

      <div class="form-group">
        <label class="form-label" for="regEmail">Alamat Email</label>
        <input type="email" id="regEmail" class="form-input" placeholder="email_anda@gmail.com" required oninput="validateEmailLive(this, 'regEmailErr')">
        <div id="regEmailErr" style="display: none; color: #f87171; font-size: 0.725rem; margin-top: 0.35rem;">Format email tidak valid.</div>
      </div>

      <div class="form-group">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <label class="form-label" for="regPassword" style="margin-bottom: 0;">Kata Sandi (Password)</label>
          <span style="font-size: 0.7rem; color: #94a3b8;">Min. 6 karakter</span>
        </div>
        <div style="position: relative; display: flex; align-items: center;">
          <input type="password" id="regPassword" class="form-input" placeholder="Buat kata sandi baru" required minlength="6" style="padding-right: 2.75rem;" oninput="validatePasswordLive(this, 'regPassErr')">
          <button type="button" onclick="togglePasswordVisibility('regPassword', 'eyeIconReg')" style="position: absolute; right: 0.75rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.25rem; font-size: 0.95rem;" title="Lihat/Sembunyikan Password">
            <span id="eyeIconReg">👁️</span>
          </button>
        </div>
        <div id="regPassErr" style="display: none; color: #f87171; font-size: 0.725rem; margin-top: 0.35rem;">Password minimal 6 karakter.</div>
      </div>

      <div class="form-group">
        <label class="form-label" for="regConfirmPassword">Konfirmasi Kata Sandi</label>
        <div style="position: relative; display: flex; align-items: center;">
          <input type="password" id="regConfirmPassword" class="form-input" placeholder="Ulangi kata sandi" required minlength="6" style="padding-right: 2.75rem;" oninput="validateConfirmLive()">
          <button type="button" onclick="togglePasswordVisibility('regConfirmPassword', 'eyeIconRegConf')" style="position: absolute; right: 0.75rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.25rem; font-size: 0.95rem;" title="Lihat/Sembunyikan Password">
            <span id="eyeIconRegConf">👁️</span>
          </button>
        </div>
        <div id="regConfirmErr" style="display: none; color: #f87171; font-size: 0.725rem; margin-top: 0.35rem;">Konfirmasi password tidak cocok.</div>
      </div>

      <button type="submit" id="btnRegSubmit" class="btn-submit">Daftar & Masuk</button>
    </form>

    <!-- 1-CLICK QUICK ACCESS FOR OWNER -->
    <div class="quick-box">
      <div class="quick-title">Akses Cepat Pemilik Server</div>
      <button type="button" class="quick-btn" onclick="quickLoginAres()">
        <span>⚡ Isi Akun <b>areskretindo@gmail.com</b></span>
        <span class="quick-badge">OWNER</span>
      </button>
    </div>

    <div style="margin-top: 1rem; padding: 0.65rem 0.85rem; background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 8px; font-size: 0.75rem; color: #7dd3fc; display: flex; align-items: center; gap: 0.5rem;">
      <span style="font-size: 1rem;">🛡️</span>
      <span><b>Keamanan Akun:</b> Validasi email & password terenkripsi PBKDF2-HMAC-SHA256 untuk proteksi server.</span>
    </div>

    <div class="notice-text">
      🔒 Autentikasi aman terenkripsi. Sesi aktif berlaku 30 hari di peramban ini.
    </div>
  </div>

  <script>
    let currentAuthMode = 'login';

    function switchAuthTab(mode) {
      currentAuthMode = mode;
      document.getElementById('tabBtnLogin').classList.toggle('active', mode === 'login');
      document.getElementById('tabBtnRegister').classList.toggle('active', mode === 'register');
      document.getElementById('loginForm').style.display = mode === 'login' ? 'block' : 'none';
      document.getElementById('registerForm').style.display = mode === 'register' ? 'block' : 'none';
      hideAlert();
    }

    function showAlert(msg, isError = true) {
      const box = document.getElementById('alertBox');
      box.textContent = msg;
      box.className = 'alert-box ' + (isError ? 'alert-error' : 'alert-success');
      box.style.display = 'block';
    }

    function hideAlert() {
      document.getElementById('alertBox').style.display = 'none';
    }

    function isValidEmail(email) {
      const re = /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/;
      return re.test(email);
    }

    function validateEmailLive(input, errId) {
      const err = document.getElementById(errId);
      const val = input.value.trim();
      if (!val) {
        if (err) err.style.display = 'none';
        input.style.borderColor = '#2d3340';
        return false;
      }
      const ok = isValidEmail(val);
      if (err) err.style.display = ok ? 'none' : 'block';
      input.style.borderColor = ok ? '#10b981' : '#ef4444';
      return ok;
    }

    function validatePasswordLive(input, errId) {
      const err = document.getElementById(errId);
      const val = input.value;
      if (!val) {
        if (err) err.style.display = 'none';
        input.style.borderColor = '#2d3340';
        return false;
      }
      const ok = val.length >= 6;
      if (err) err.style.display = ok ? 'none' : 'block';
      input.style.borderColor = ok ? '#10b981' : '#ef4444';
      return ok;
    }

    function validateConfirmLive() {
      const p1 = document.getElementById('regPassword').value;
      const p2 = document.getElementById('regConfirmPassword');
      const err = document.getElementById('regConfirmErr');
      if (!p2.value) {
        if (err) err.style.display = 'none';
        p2.style.borderColor = '#2d3340';
        return false;
      }
      const ok = (p1 === p2.value);
      if (err) err.style.display = ok ? 'none' : 'block';
      p2.style.borderColor = ok ? '#10b981' : '#ef4444';
      return ok;
    }

    function togglePasswordVisibility(inputId, iconId) {
      const input = document.getElementById(inputId);
      const icon = document.getElementById(iconId);
      if (!input) return;
      if (input.type === 'password') {
        input.type = 'text';
        if (icon) icon.textContent = '🙈';
      } else {
        input.type = 'password';
        if (icon) icon.textContent = '👁️';
      }
    }

    async function handleLogin(e) {
      if (e && e.preventDefault) e.preventDefault();
      const email = document.getElementById('loginEmail').value.trim();
      const password = document.getElementById('loginPassword').value;

      // VALIDASI EMAIL
      if (!email) {
        showAlert('Silakan masukkan alamat email Anda.');
        document.getElementById('loginEmail').focus();
        return;
      }
      if (!isValidEmail(email)) {
        showAlert('Format email tidak valid. Harap gunakan format lengkap (contoh: ares@gmail.com).');
        document.getElementById('loginEmail').focus();
        return;
      }

      // VALIDASI PASSWORD
      if (!password) {
        showAlert('Silakan masukkan kata sandi (password).');
        document.getElementById('loginPassword').focus();
        return;
      }
      if (password.length < 6) {
        showAlert('Kata sandi minimal 6 karakter.');
        document.getElementById('loginPassword').focus();
        return;
      }

      // Non-blocking permission request
      if (window.Notification && Notification.permission === 'default') {
        Notification.requestPermission().catch(() => {});
      }

      const btn = document.getElementById('btnLoginSubmit');
      btn.disabled = true;
      btn.textContent = 'Memverifikasi...';
      hideAlert();

      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password })
        });
        const data = await res.json();
        if (data.success) {
          showAlert('Berhasil masuk! Mengarahkan ke dashboard...', false);
          setTimeout(() => {
            window.location.href = '/solar';
          }, 400);
        } else {
          showAlert(data.error || 'Gagal masuk. Periksa kembali email dan password Anda.');
          btn.disabled = false;
          btn.textContent = 'Masuk ke Server';
        }
      } catch (err) {
        showAlert('Kesalahan koneksi ke server: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Masuk ke Server';
      }
    }

    async function handleRegister(e) {
      if (e && e.preventDefault) e.preventDefault();
      const email = document.getElementById('regEmail').value.trim();
      const name = document.getElementById('regName').value.trim();
      const password = document.getElementById('regPassword').value;
      const confirmPassword = document.getElementById('regConfirmPassword').value;

      // VALIDASI EMAIL
      if (!email) {
        showAlert('Silakan masukkan alamat email.');
        document.getElementById('regEmail').focus();
        return;
      }
      if (!isValidEmail(email)) {
        showAlert('Format email tidak valid (contoh: nama@domain.com).');
        document.getElementById('regEmail').focus();
        return;
      }

      // VALIDASI PASSWORD
      if (!password) {
        showAlert('Silakan masukkan kata sandi baru.');
        document.getElementById('regPassword').focus();
        return;
      }
      if (password.length < 6) {
        showAlert('Kata sandi minimal 6 karakter.');
        document.getElementById('regPassword').focus();
        return;
      }
      if (password !== confirmPassword) {
        showAlert('Konfirmasi kata sandi tidak cocok dengan kata sandi yang dibuat.');
        document.getElementById('regConfirmPassword').focus();
        return;
      }

      // Non-blocking permission request
      if (window.Notification && Notification.permission === 'default') {
        Notification.requestPermission().catch(() => {});
      }

      const btn = document.getElementById('btnRegSubmit');
      btn.disabled = true;
      btn.textContent = 'Mendaftarkan...';
      hideAlert();

      try {
        const res = await fetch('/api/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password, name })
        });
        const data = await res.json();
        if (data.success) {
          showAlert('Pendaftaran berhasil! Mengarahkan ke dashboard...', false);
          setTimeout(() => {
            window.location.href = '/solar';
          }, 400);
        } else {
          showAlert(data.error || 'Gagal mendaftar akun baru.');
          btn.disabled = false;
          btn.textContent = 'Daftar & Masuk';
        }
      } catch (err) {
        showAlert('Kesalahan koneksi ke server: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Daftar & Masuk';
      }
    }

    async function quickLoginAres() {
      document.getElementById('loginEmail').value = 'areskretindo@gmail.com';
      switchAuthTab('login');
      const pass = document.getElementById('loginPassword');
      pass.focus();
      showAlert('Email pemilik terisi. Silakan masukkan kata sandi Anda.', false);
    }
  </script>
</body>
</html>
"""
