use std::fmt::{Display, Formatter};

#[derive(Debug, Clone)]
pub enum ReaPeaksError {
    Io(String),
    Truncated,
    InvalidMagic([u8; 4]),
    InvalidHeader(&'static str),
    Unsupported(&'static str),
    InvalidArgument(&'static str),
}

impl Display for ReaPeaksError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(s) => write!(f, "I/O error: {s}"),
            Self::Truncated => write!(f, "truncated .ReaPeaks file"),
            Self::InvalidMagic(m) => write!(f, "invalid .ReaPeaks magic: {:?}", m),
            Self::InvalidHeader(s) => write!(f, "invalid .ReaPeaks header: {s}"),
            Self::Unsupported(s) => write!(f, "unsupported .ReaPeaks feature: {s}"),
            Self::InvalidArgument(s) => write!(f, "invalid argument: {s}"),
        }
    }
}

impl std::error::Error for ReaPeaksError {}

impl From<std::io::Error> for ReaPeaksError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value.to_string())
    }
}

pub type Result<T> = std::result::Result<T, ReaPeaksError>;
