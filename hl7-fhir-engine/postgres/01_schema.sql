-- ============================================================
-- SIL Schema - HL7 FHIR Engine Legacy Database
-- ============================================================

CREATE SCHEMA IF NOT EXISTS sil;

-- ============================================================
-- DPEDIDOS - Order Header / Patient Request
-- ============================================================
CREATE TABLE sil.dpedidos (
    numpedido       VARCHAR(64)   NOT NULL PRIMARY KEY,
    numprocesso     NUMERIC(15),
    episodio        NUMERIC(20),
    modulo          VARCHAR(3),
    designamodulo   VARCHAR(30),
    especialid      NUMERIC(12),
    designaespecial VARCHAR(40),
    idser           VARCHAR(10),
    servico         VARCHAR(60),
    datapedido      DATE,
    posto           VARCHAR(20),
    dociddoente     NUMERIC(12),
    nomedoente      VARCHAR(100),
    datanascimento  DATE,
    sexo            VARCHAR(1),
    modalidade      VARCHAR(20),
    numexame        VARCHAR(64),
    verificado      NUMERIC(1),
    avisado         NUMERIC(1),
    marcado         NUMERIC(1),
    recolhido       NUMERIC(1),
    fechado         NUMERIC(1),
    segura          NUMERIC(1),
    utiliza         VARCHAR(32),
    utilizamarca    VARCHAR(32),
    utilizarecolha  VARCHAR(32),
    tipoanalise     VARCHAR(1),
    medico          VARCHAR(40),
    serv            VARCHAR(10),
    dataverificacao DATE,
    datamarcado     DATE,
    datamarcacao    DATE,
    horamarcacao    DATE,
    datarecolha     DATE,
    datachegada     DATE,
    datachamada     DATE,
    dataexame       DATE,
    urgente         NUMERIC(3),
    observacoes     VARCHAR(2000),
    infclinica      VARCHAR(4000),
    contraindic     VARCHAR(2000),
    sugmarcacao     VARCHAR(2000),
    pedidomae       VARCHAR(64),
    postoagenda     VARCHAR(30),
    ref_entrada     VARCHAR(20),
    bloqagenda      NUMERIC(1),
    triagem         VARCHAR(10)
);

-- ============================================================
-- PEDIDOS - Order Lines
-- ============================================================
CREATE TABLE sil.pedidos (
    numpedido           VARCHAR(64)  NOT NULL,
    ordem               NUMERIC(4)   NOT NULL,
    idser               VARCHAR(10),
    modalidade          VARCHAR(20),
    codprotocolo        VARCHAR(10),
    designacaoprotocolo VARCHAR(90),
    codprop             VARCHAR(1),
    verificado          NUMERIC(1),
    dataverificacao     DATE,
    pedidofilho         VARCHAR(64),
    cod_pedido          VARCHAR(20),
    des_pedido          VARCHAR(200),
    notas_linhas        VARCHAR(2000),
    cancelar            NUMERIC,
    datamsg             DATE,
    numactomed          NUMERIC,
    id_bdnr             VARCHAR(30),
    id_requisicao       VARCHAR(30),
    cod_mcdt            VARCHAR(51),
    pin_agendamento     VARCHAR(10),
    PRIMARY KEY (numpedido, ordem)
);

-- ============================================================
-- DEXAMES - Exam Header
-- ============================================================
CREATE TABLE sil.dexames (
    numexame           VARCHAR(64)  NOT NULL,
    versao             NUMERIC(4)   NOT NULL,
    numprocesso        NUMERIC(15),
    episodio           NUMERIC(20),
    modulo             VARCHAR(3),
    designamodulo      VARCHAR(30),
    especialid         NUMERIC(7),
    designaespecial    VARCHAR(40),
    idser              VARCHAR(10)  NOT NULL,
    servico            VARCHAR(100),
    datapedido         DATE,
    dataexame          DATE,
    dociddoente        NUMERIC(12),
    nomedoente         VARCHAR(100),
    datanascimento     DATE,
    sexo               VARCHAR(1),
    pathrelatorio      VARCHAR(100),
    grupo              VARCHAR(20)  NOT NULL,
    numpedido          VARCHAR(64),
    verificado         NUMERIC(1),
    facturado          NUMERIC(1),
    imagem             NUMERIC(1),
    relatorio          NUMERIC(2),
    fechado            NUMERIC(1),
    segura             NUMERIC(1),
    utiliza            VARCHAR(32),
    tipoanalise        VARCHAR(1),
    medico             VARCHAR(40),
    serv               VARCHAR(10),
    dataverificacao    DATE,
    datafacturacao     DATE,
    datarelat          DATE,
    datafecho          DATE,
    dataimagem         DATE,
    subsistema         NUMERIC(6),
    proveniencia       NUMERIC(2),
    listaestudos       VARCHAR(100),
    ccusto             NUMERIC(20),
    listatecnicos      VARCHAR(100),
    posto              VARCHAR(20),
    modalidade         VARCHAR(20),
    dauditado          DATE,
    dexaminado         DATE,
    numeroprov         VARCHAR(64),
    dta_alta           DATE,
    nomefich           VARCHAR(64),
    dataimpressao      DATE,
    horapedido         VARCHAR(10),
    horaexame          VARCHAR(10),
    idserori           VARCHAR(40),
    nori               NUMERIC(1),
    bodypart           VARCHAR(500),
    dta_consulta       DATE,
    next_especialidade VARCHAR(50),
    mrelat             NUMERIC,
    dataversao         DATE,
    privado            VARCHAR(1),
    req_idser          VARCHAR(100),
    aoucexame          NUMERIC(1),
    PRIMARY KEY (numexame, idser, grupo, versao)
);

-- ============================================================
-- EXAMES - Exam Lines / Individual Tests
-- ============================================================
CREATE TABLE sil.exames (
    numexame           VARCHAR(64)  NOT NULL,
    versao             NUMERIC(4)   NOT NULL,
    ordem              NUMERIC(4)   NOT NULL,
    validade           NUMERIC(3),
    idser              VARCHAR(10)  NOT NULL,
    grupo              VARCHAR(20)  NOT NULL,
    codigif            VARCHAR(10),
    designacaoigif     VARCHAR(200),
    codigoanalise      VARCHAR(10)  NOT NULL,
    designacaoanalise  VARCHAR(200),
    codprop            VARCHAR(1),
    verificado         NUMERIC(1),
    facturado          NUMERIC(1),
    dataverificacao    DATE,
    datafacturacao     DATE,
    tipoanestesia      VARCHAR(50),
    medicoanestesia    VARCHAR(50),
    idserori           VARCHAR(10),
    id_bdnr            VARCHAR(30),
    id_requisicao      VARCHAR(30),
    cod_mcdt           VARCHAR(51),
    pin_agendamento    VARCHAR(10),
    PRIMARY KEY (idser, grupo, numexame, codigoanalise, versao, ordem)
);

-- ============================================================
-- RESULTADOS - result values per ordered test
--
-- NOT part of the SIL schema supplied for this work: SIL.EXAMES
-- records which analyses were performed but holds no result values.
-- This table is a documented addition to the demonstration database
-- so that Observations can carry numeric values with units (UCUM) and
-- coded qualitative values (SNOMED CT). Where a real SIL stores
-- results must be confirmed before production use.
--
-- tiporesultado: NM numeric, CE coded, TX free text,
--                CO report conclusion (-> DiagnosticReport.conclusion)
-- estado:        F final, P preliminary, C corrected
-- flag:          N, L, H, LL, HH, A, POS, NEG
-- ============================================================
CREATE TABLE sil.resultados (
    idser               VARCHAR(10)   NOT NULL,
    grupo               VARCHAR(20)   NOT NULL,
    numexame            VARCHAR(64)   NOT NULL,
    codigoanalise       VARCHAR(10)   NOT NULL,
    versao              NUMERIC(4)    NOT NULL,
    ordem               NUMERIC(4)    NOT NULL,
    seq                 NUMERIC(4)    NOT NULL,
    codparametro        VARCHAR(20)   NOT NULL,
    designacaoparametro VARCHAR(200),
    tiporesultado       VARCHAR(2)    NOT NULL CHECK (tiporesultado IN ('NM','CE','TX','CO')),
    valornumerico       NUMERIC(18,6),
    comparador          VARCHAR(2),
    unidade             VARCHAR(30),
    codvalor            VARCHAR(30),
    valortexto          VARCHAR(4000),
    refmin              NUMERIC(18,6),
    refmax              NUMERIC(18,6),
    reftexto            VARCHAR(100),
    flag                VARCHAR(3),
    estado              VARCHAR(1)    DEFAULT 'F' CHECK (estado IN ('F','P','C')),
    dataresultado       TIMESTAMP,
    validadopor         VARCHAR(40),
    PRIMARY KEY (idser, grupo, numexame, codigoanalise, versao, ordem, seq),
    FOREIGN KEY (idser, grupo, numexame, codigoanalise, versao, ordem)
        REFERENCES sil.exames (idser, grupo, numexame, codigoanalise, versao, ordem)
);

CREATE INDEX ix_resultados_exame ON sil.resultados (numexame, idser, grupo, versao);
