"""
database.py - SQLAlchemy models mirroring the SIL legacy schema.

The engine connects with the sil_reader role, which has SELECT on the
sil schema and nothing else (postgres/07_roles.sql), and every session
is additionally opened read-only. Requirement R1 (no writes to the
legacy system) is therefore enforced by the database, not by convention.
"""
from datetime import date
from sqlalchemy import (
    create_engine, Column, String, Numeric, Date, DateTime, Text
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool
import os

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://sil_reader:sil_reader_pass@postgres:5432/sildb"
)

# default_transaction_read_only: a second, connection-level guard on top of
# the role's privileges (a write raises "cannot execute ... in a read-only
# transaction" even if a privilege were granted by mistake).
engine = create_engine(
    DB_URL, poolclass=NullPool, echo=False,
    connect_args={"options": "-c default_transaction_read_only=on"},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class DPedido(Base):
    __tablename__ = "dpedidos"
    __table_args__ = {"schema": "sil"}

    numpedido       = Column(String(64), primary_key=True)
    numprocesso     = Column(Numeric(15))
    episodio        = Column(Numeric(20))
    modulo          = Column(String(3))
    designamodulo   = Column(String(30))
    especialid      = Column(Numeric(12))
    designaespecial = Column(String(40))
    idser           = Column(String(10))
    servico         = Column(String(60))
    datapedido      = Column(Date)
    posto           = Column(String(20))
    dociddoente     = Column(Numeric(12))
    nomedoente      = Column(String(100))
    datanascimento  = Column(Date)
    sexo            = Column(String(1))
    modalidade      = Column(String(20))
    numexame        = Column(String(64))
    verificado      = Column(Numeric(1))
    avisado         = Column(Numeric(1))
    marcado         = Column(Numeric(1))
    recolhido       = Column(Numeric(1))
    fechado         = Column(Numeric(1))
    segura          = Column(Numeric(1))
    utiliza         = Column(String(32))
    tipoanalise     = Column(String(1))
    medico          = Column(String(40))
    serv            = Column(String(10))
    dataverificacao = Column(Date)
    dataexame       = Column(Date)
    urgente         = Column(Numeric(3))
    observacoes     = Column(Text)
    infclinica      = Column(Text)
    contraindic     = Column(Text)
    triagem         = Column(String(10))


class Pedido(Base):
    __tablename__ = "pedidos"
    __table_args__ = {"schema": "sil"}

    numpedido           = Column(String(64), primary_key=True)
    ordem               = Column(Numeric(4),  primary_key=True)
    idser               = Column(String(10))
    modalidade          = Column(String(20))
    codprotocolo        = Column(String(10))
    designacaoprotocolo = Column(String(90))
    codprop             = Column(String(1))
    verificado          = Column(Numeric(1))
    dataverificacao     = Column(Date)
    cod_pedido          = Column(String(20))
    des_pedido          = Column(String(200))
    notas_linhas        = Column(String(2000))
    cancelar            = Column(Numeric)
    id_bdnr             = Column(String(30))
    id_requisicao       = Column(String(30))
    cod_mcdt            = Column(String(51))


class DExame(Base):
    __tablename__ = "dexames"
    __table_args__ = {"schema": "sil"}

    numexame        = Column(String(64), primary_key=True)
    versao          = Column(Numeric(4),  primary_key=True)
    idser           = Column(String(10),  primary_key=True)
    grupo           = Column(String(20),  primary_key=True)
    numprocesso     = Column(Numeric(15))
    episodio        = Column(Numeric(20))
    modulo          = Column(String(3))
    designamodulo   = Column(String(30))
    especialid      = Column(Numeric(7))
    designaespecial = Column(String(40))
    servico         = Column(String(100))
    datapedido      = Column(Date)
    dataexame       = Column(Date)
    dociddoente     = Column(Numeric(12))
    nomedoente      = Column(String(100))
    datanascimento  = Column(Date)
    sexo            = Column(String(1))
    posto           = Column(String(20))
    numpedido       = Column(String(64))
    verificado      = Column(Numeric(1))
    facturado       = Column(Numeric(1))
    imagem          = Column(Numeric(1))
    relatorio       = Column(Numeric(2))
    fechado         = Column(Numeric(1))
    medico          = Column(String(40))
    serv            = Column(String(10))
    dataverificacao = Column(Date)
    datafacturacao  = Column(Date)
    datarelat       = Column(Date)
    datafecho       = Column(Date)
    subsistema      = Column(Numeric(6))
    proveniencia    = Column(Numeric(2))
    modalidade      = Column(String(20))
    bodypart        = Column(String(500))


class Exame(Base):
    __tablename__ = "exames"
    __table_args__ = {"schema": "sil"}

    numexame          = Column(String(64), primary_key=True)
    versao            = Column(Numeric(4),  primary_key=True)
    ordem             = Column(Numeric(4),  primary_key=True)
    idser             = Column(String(10),  primary_key=True)
    grupo             = Column(String(20),  primary_key=True)
    codigoanalise     = Column(String(10),  primary_key=True)
    validade          = Column(Numeric(3))
    codigif           = Column(String(10))
    designacaoigif    = Column(String(200))
    designacaoanalise = Column(String(200))
    codprop           = Column(String(1))
    verificado        = Column(Numeric(1))
    facturado         = Column(Numeric(1))
    dataverificacao   = Column(Date)
    datafacturacao    = Column(Date)
    id_bdnr           = Column(String(30))
    id_requisicao     = Column(String(30))
    cod_mcdt          = Column(String(51))


class Resultado(Base):
    """Result values per ordered test. NOT part of the SIL schema supplied
    for this work (which stores no result values); added to the
    demonstration database so Observations can carry real values."""
    __tablename__ = "resultados"
    __table_args__ = {"schema": "sil"}

    idser               = Column(String(10),  primary_key=True)
    grupo               = Column(String(20),  primary_key=True)
    numexame            = Column(String(64),  primary_key=True)
    codigoanalise       = Column(String(10),  primary_key=True)
    versao              = Column(Numeric(4),  primary_key=True)
    ordem               = Column(Numeric(4),  primary_key=True)
    seq                 = Column(Numeric(4),  primary_key=True)
    codparametro        = Column(String(20))
    designacaoparametro = Column(String(200))
    tiporesultado       = Column(String(2))     # NM numeric, CE coded, TX text, CO report conclusion
    valornumerico       = Column(Numeric(18, 6))
    comparador          = Column(String(2))
    unidade             = Column(String(30))
    codvalor            = Column(String(30))
    valortexto          = Column(Text)
    refmin              = Column(Numeric(18, 6))
    refmax              = Column(Numeric(18, 6))
    reftexto            = Column(String(100))
    flag                = Column(String(3))
    estado              = Column(String(1))     # F final, P preliminary, C corrected
    dataresultado       = Column(DateTime)
    validadopor         = Column(String(40))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
