import { makeStyles } from '@fluentui/react-components'

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
})

export default useStyles
