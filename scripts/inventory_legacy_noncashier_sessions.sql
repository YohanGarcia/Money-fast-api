-- FM-FUNCTIONAL-018-R2 — INVENTARIO DE SOLO LECTURA de jornadas abiertas cuyo dueño no es cajero.
-- No modifica nada. Ejecutar con un usuario de solo lectura. Ver docs/FM-FUNCTIONAL-018-R2-sobrantes-reversos-jornadas.md

-- 1) Jornadas abiertas con dueño que no es cajero, con su saldo esperado y conciliación contra el saldo guardado.
SELECT s.id                       AS session_id,
       b.company_id,
       b.branch_id,
       s.cashier_id,
       u.full_name,
       u.email,
       u.role                     AS dueño_rol,
       s.state,
       s.opened_at,
       s.balance                  AS saldo_guardado,
       COALESCE(m.suma, 0)        AS suma_movimientos,
       s.balance - COALESCE(m.suma, 0) AS diferencia_vs_movimientos
  FROM cash_sessions s
  JOIN cash_boxes b ON b.id = s.box_id
  JOIN users u      ON u.id = s.cashier_id
  LEFT JOIN (SELECT session_id, SUM(amount) AS suma FROM cash_movements GROUP BY session_id) m ON m.session_id = s.id
 WHERE s.state = 'open'
   AND COALESCE(u.role::text, '') <> 'cashier'
 ORDER BY b.company_id, b.branch_id, s.id;

-- 2) Entregas de cobradores y sobrantes pendientes ligados a esas jornadas (bloquean el cierre).
SELECT d.id AS delivery_id, d.box_id, d.session_id, d.collector_id, d.declared, d.state
  FROM cash_deliveries d
  JOIN cash_sessions s ON s.id = d.session_id
  JOIN users u ON u.id = s.cashier_id
 WHERE s.state = 'open'
   AND COALESCE(u.role::text, '') <> 'cashier'
   AND d.state IN ('pending', 'surplus_review')
 ORDER BY d.id;

-- 3) Verificación posterior al cierre supervisado: debe devolver 0 filas.
SELECT count(*) AS jornadas_bloqueadas
  FROM cash_sessions s JOIN users u ON u.id = s.cashier_id
 WHERE s.state = 'open' AND COALESCE(u.role::text, '') <> 'cashier';
