import { makeStyles, tokens } from '@fluentui/react-components'


const waveKeyframes = {
  "0%, 75%, 100%": {
    height: "0px",
    backgroundColor: tokens.colorBrandForegroundInvertedHover,
  },
  "25%": {
    height: "20px",
    backgroundColor: tokens.colorBrandForegroundInvertedPressed,
  },
};

const useStyles = makeStyles({
  root: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
  },
  card: {
    width: 'calc(100% - 40px)',
    height: 'inherit',
    background: 'rgba(132, 102, 196, 0.18)',
    border: '1px solid rgba(183, 164, 221, 0.5)',
    borderRadius: '12px',
    padding: '20px',
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    boxShadow: '0 6px 20px rgba(15, 12, 45, 0.28)',
    overflowY: 'auto',
  },
  inputRow: {
    display: 'flex',
    gap: '8px',
    marginTop: '8px',
    paddingTop: '16px',
  },
  input: {
    flex: 1,
  },
  hintText: {
    width: 'fit-content',
    maxWidth: '40%',
    textAlign: 'left',
    color: 'rgba(255,255,255,0.9)',
    fontSize: '14px',
    margin: '0',
    borderRadius: '4px',
    padding: '8px',
    background: 'rgba(255, 255, 255, 0.1)',
  },
  chatBody: {
    maxHeight: '50vh',
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
  },
  messageItemUser: {
    textAlign: 'right',
    marginBottom: '4px',
  },
  messageItemAssistant: {
    textAlign: 'left',
    marginBottom: '4px',
  },
  messageBubble: {
    display: 'inline-block',
    padding: '8px',
    borderRadius: '4px',
    color: '#fff',
  },
  loadingRow: {
    display: 'flex',
    gap: '8px',
    alignItems: 'baseline',
  },
  loadingIndicatorContainer: {
    textAlign: 'left',
    marginBottom: '4px',
    display: 'flex',
    gap: '4px',
    height: '90px',
    alignItems: 'flex-end',
  },
  thinkingText: {
    paddingLeft: '8px',
  },
  bar: {
    width: "30px",
    height: "0px",
    backgroundColor: tokens.colorBrandBackground,
    animationName: waveKeyframes, 
    animationDuration: "1s",
    animationIterationCount: "infinite",
    animationTimingFunction: "ease-in-out",
  },
  bar2: {
    animationDelay: "0.30s",
  },
  bar3: {
    animationDelay: "0.70s",
  },
})

export default useStyles
