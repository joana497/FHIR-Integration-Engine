'use strict';

const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');
const crypto = require('crypto');
const logger = require('../utils/logger');
const auditRepo = require('./auditRepo');

// ─── Config ───────────────────────────────────────────────────────────────────
// In production the secret MUST come from a secret manager, not source code.
const JWT_SECRET = process.env.JWT_SECRET || 'dev-only-secret-change-me';
const TOKEN_TTL_SECONDS = parseInt(process.env.TOKEN_TTL || '1800', 10); // 30 min
const ISSUER = process.env.JWT_ISSUER || 'https://auth.example.org';
const AUDIENCE = process.env.JWT_AUDIENCE || 'https://fhir.example.org';

// ─── Demonstration accounts ───────────────────────────────────────────────────
// Passwords are stored only as bcrypt hashes (cost 12, salted). The clear-text
// demonstration passwords appear in the README, never in this file. A real
// deployment would delegate authentication to an identity provider.
const USERS = {
  admin: {
    passwordHash: '$2a$12$4szJAuUfGCOILCvDr4o59.iI77wj4kn6g4W7twKbhy.YCu8byNopK',
    role: 'admin', name: 'Administrador (demo)',
    scopes: ['fhir/*.read', 'fhir/*.write', 'fhir/*.delete', 'admin.*', 'audit.read', 'sync.*'],
  },
  medico: {
    passwordHash: '$2a$12$WSe/G1EWV.80y7gE7CA7OeSr1mA87Bl9cbiuo3L4Xz3YNT5YYSXCO',
    role: 'medico', name: 'Médica (demo)',
    scopes: ['fhir/Patient.read', 'fhir/DiagnosticReport.read', 'fhir/ServiceRequest.read'],
  },
  tecnico: {
    passwordHash: '$2a$12$1.w9QyhL4r38n.yWJ.YZ8O/4ylTc11NY0101o3Qup/vMfzgHErL4m',
    role: 'tecnico', name: 'Técnico (demo)',
    scopes: ['fhir/*.read', 'fhir/Bundle.write', 'sync.*'],
  },
  auditor: {
    passwordHash: '$2a$12$E1Fu0SYzLWEM5l/R3tPX0O3ELXwqaplUTRi3rRUpOYNK.xJfVg5za',
    role: 'auditor', name: 'Auditor (demo)',
    scopes: ['fhir/*.read', 'audit.read'],
  },
};
// Compared against when the user does not exist, so that response time does
// not reveal which usernames are valid.
const DUMMY_HASH = '$2a$12$S1Dzw7ztR01m/b61fht7PejHbHZgbKs7RGjk4jLKn2ct/hRg7Swp6';

// ─── RBAC permission matrix ───────────────────────────────────────────────────
const PERMISSIONS = {
  admin:   { read: true,  write: true,  del: true,  sync: true,  admin: true,  audit: true },
  medico:  { read: true,  write: false, del: false, sync: false, admin: false, audit: false },
  tecnico: { read: true,  write: true,  del: false, sync: true,  admin: false, audit: false },
  auditor: { read: true,  write: false, del: false, sync: false, admin: false, audit: true },
};

// ─── Security events (persisted, see auditRepo.recordSecurity) ────────────────
function securityEvent(req, level, message, statusCode, userSub) {
  const entry = {
    level,
    user_sub: userSub || req?.user?.sub || null,
    message,
    ip_address: req?.ip || req?.socket?.remoteAddress || null,
    method: req?.method || null,
    path: req?.originalUrl || req?.path || null,
    status_code: statusCode != null ? statusCode : null,
  };
  logger.info('security_event', entry);
  return auditRepo.recordSecurity(entry);
}

// ─── Login ────────────────────────────────────────────────────────────────────
async function login(username, password, role) {
  const user = Object.prototype.hasOwnProperty.call(USERS, username || '') ? USERS[username] : null;
  const ok = await bcrypt.compare(password || '', user ? user.passwordHash : DUMMY_HASH);
  if (!user || !ok) return { ok: false, error: 'Credenciais inválidas' };
  if (role && user.role !== role) return { ok: false, error: 'Perfil incorreto para este utilizador' };

  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: ISSUER, sub: username, aud: AUDIENCE, iat: now, exp: now + TOKEN_TTL_SECONDS,
    jti: crypto.randomUUID(), role: user.role, name: user.name, scope: user.scopes.join(' '),
    fhir_version: 'R4',
  };
  return {
    ok: true,
    access_token: jwt.sign(payload, JWT_SECRET, { algorithm: 'HS256' }),
    token_type: 'Bearer',
    expires_in: TOKEN_TTL_SECONDS,
    role: user.role,
    name: user.name,
    scopes: user.scopes,
  };
}

function verify(token) {
  try {
    return { ok: true, payload: jwt.verify(token, JWT_SECRET, { issuer: ISSUER, audience: AUDIENCE, algorithms: ['HS256'] }) };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// ─── Express middleware: require a valid JWT ──────────────────────────────────
function requireAuth(req, res, next) {
  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) {
    securityEvent(req, 'DENY', `Pedido sem token — ${req.method} ${req.path}`, 401);
    return res.status(401).json({ error: 'Token em falta', code: 'UNAUTHENTICATED' });
  }
  const result = verify(token);
  if (!result.ok) {
    securityEvent(req, 'DENY', `Token inválido (${result.error}) — ${req.method} ${req.path}`, 401);
    return res.status(401).json({ error: 'Token inválido ou expirado', code: 'INVALID_TOKEN' });
  }
  req.user = result.payload;
  next();
}

// ─── Express middleware factory: require a specific RBAC permission ──────────
// Every authorised request is written to the access log before it is served.
// If the access cannot be recorded anywhere durable (database and outbox
// both down), the request is refused: no unlogged access to health data.
function requirePermission(perm) {
  return async (req, res, next) => {
    const role = req.user?.role;
    if (!PERMISSIONS[role]?.[perm]) {
      securityEvent(req, 'DENY', `Acesso negado a ${perm} — ${req.method} ${req.path}`, 403);
      return res.status(403).json({ error: `Permissão insuficiente: requer '${perm}'`, code: 'FORBIDDEN', role });
    }
    const where = await securityEvent(req, 'OK', `Acesso autorizado a ${perm} — ${req.method} ${req.path}`);
    if (!where) {
      return res.status(503).json({ error: 'Registo de acessos indisponível — pedido recusado', code: 'AUDIT_UNAVAILABLE' });
    }
    next();
  };
}

module.exports = { login, verify, requireAuth, requirePermission, securityEvent, PERMISSIONS, USERS };
