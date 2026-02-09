-- =============================================================================
-- Oracle 해시 검증용 NUMBER 컬럼 정규화 SELECT 조각 생성 (딕셔너리 기반)
-- all_tab_columns의 data_precision / data_scale 을 읽어 TO_CHAR 포맷 자동 생성
-- =============================================================================
-- 사용법: SCHEMA_NAME, TABLE_NAME 치환 후 실행 → 출력된 조각을 SELECT에 붙여 사용
-- =============================================================================

SET SERVEROUTPUT ON SIZE UNLIMITED

DECLARE
    CURSOR c_num_cols IS
        SELECT
            column_id,
            column_name,
            data_precision,
            data_scale
        FROM all_tab_columns
        WHERE owner       = 'SCHEMA_NAME'
          AND table_name  = 'TABLE_NAME'
          AND data_type   = 'NUMBER'
          -- fallback 컬럼 제외 시 아래 주석 해제 (precision/scale 없는 NUMBER 제외)
          -- AND data_precision IS NOT NULL
          -- AND data_scale IS NOT NULL
        ORDER BY column_id;

    v_fmt        VARCHAR2(200);
    v_expr       VARCHAR2(500);
    v_int_digits NUMBER;
BEGIN
    FOR r IN c_num_cols LOOP

        -- precision / scale 이 정의된 경우
        IF r.data_precision IS NOT NULL AND r.data_scale IS NOT NULL THEN
            v_int_digits := r.data_precision - r.data_scale;

            v_fmt :=
                'FM'
                || RPAD('9', GREATEST(1, v_int_digits) - 1, '9')
                || '0'
                || CASE
                       WHEN r.data_scale > 0
                       THEN '.' || RPAD('0', r.data_scale, '0')
                       ELSE ''
                   END;

            v_expr :=
                'NVL(TO_CHAR(' || r.column_name || ', '''
                || v_fmt || '''), ''<NULL>'') AS '
                || r.column_name;

        -- precision / scale 없는 NUMBER (fallback)
        ELSE
            v_expr :=
                'NVL(TO_CHAR(' || r.column_name || ', ''TM9''), ''<NULL>'') AS '
                || r.column_name || ' /* FALLBACK */';
        END IF;

        DBMS_OUTPUT.PUT_LINE(v_expr || ',');
    END LOOP;
END;
/

-- =============================================================================
-- 출력 예시 (컬럼: ITM_CAPA_QTY NUMBER(15,5), CNT NUMBER(5,0), FLEX_NUM NUMBER)
-- =============================================================================
-- NVL(TO_CHAR(ITM_CAPA_QTY, 'FM9999999990.00000'), '<NULL>') AS ITM_CAPA_QTY,
-- NVL(TO_CHAR(CNT, 'FM99990'), '<NULL>') AS CNT,
-- NVL(TO_CHAR(FLEX_NUM, 'TM9'), '<NULL>') AS FLEX_NUM /* FALLBACK */,
-- =============================================================================
-- 해시용 canonical row 예: SELECT col1||'|'||col2||'|'||... FROM table ...
-- CHAR/DATE 컬럼은 별도 커서로 처리
-- =============================================================================
